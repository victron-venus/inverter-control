#!/usr/bin/env python3
"""
Victron D-Bus Interface
Fast D-Bus access for grid control and monitoring
"""

import logging
import math
import re
import subprocess
import threading
import time
from typing import Any

from .config import INVERTER_STATES, TASMOTA_DBUS_SERVICES

logger = logging.getLogger("inverter-control")

# D-Bus path constants (duplicated across methods)
DC_CURRENT_PATH = "/Dc/0/Current"
SETTINGS_SERVICE = "com.victronenergy.settings"
HUB4_MODE_PATH = "/Settings/CGwacs/Hub4Mode"
SYSTEM_SERVICE = "com.victronenergy.system"
GET_VALUE_METHOD = "com.victronenergy.BusItem.GetValue"
PRINT_REPLY_LITERAL = "--print-reply=literal"
TASMOTA_ENERGY_FORWARD_PATH = "/Ac/Energy/Forward"

# Battery chains with per-cell data, polled as full tree queries in the
# background. Reading each Cell/N/Voltage with a separate dbus-send subprocess
# (as get_battery_cell_data used to) is ~72 subprocess calls per cycle which
# blows the 5s cycle watchdog on a slow RPi.
BATTERY_CELL_SERVICES = [
    "com.victronenergy.battery.dbus-mqtt-chain1",
    "com.victronenergy.battery.dbus-mqtt-chain2",
]
# How often the background poller refreshes the cell-data cache.
CELL_DATA_POLL_INTERVAL = 30

# =============================================================================
# BATTERY SOC CALCULATION (ported from HA template sensors)
# =============================================================================
# This replaces the HA-based corrected_battery_soc calculation for independence.
# Source: HA template sensor "Corrected Battery SOC" + compensation_sensor_battery_voltage

# Polynomial coefficients for voltage -> SOC conversion (5th degree, highest first)
# From: sensor.compensation_sensor_battery_voltage coefficients attribute
# SOC = c0*V^5 + c1*V^4 + c2*V^3 + c3*V^2 + c4*V + c5
BATTERY_VOLTAGE_TO_SOC_COEFFS = (
    0.004273352289848183,  # V^5
    -1.1946101528489494,  # V^4
    131.15278553768547,  # V^3
    -7086.612266200085,  # V^2
    188790.53434597014,  # V^1
    -1986209.3055883816,  # V^0 (constant)
)

# Battery parameters for load correction (Coulomb counting approximation)
# From: HA template sensor "Corrected Battery SOC"
BATTERY_CAPACITY_CHARGE_AH = 280.0  # Ah when charging
BATTERY_CAPACITY_DISCHARGE_AH = 180.0  # Ah when discharging
BATTERY_ROUNDTRIP_EFFICIENCY = 0.95  # 95%


def _voltage_to_soc(voltage: float) -> float:
    """
    Convert battery voltage to SOC using 5th-degree polynomial.
    Coefficients from HA compensation_sensor_battery_voltage.
    """
    try:
        v = float(voltage)
        # Clamp to reasonable voltage range for LiFePO4 (16S ~ 48V nominal)
        if v < 40.0 or v > 58.4:
            return 0.0
        # Horner's method for polynomial evaluation
        soc = BATTERY_VOLTAGE_TO_SOC_COEFFS[0]
        for coeff in BATTERY_VOLTAGE_TO_SOC_COEFFS[1:]:
            soc = soc * v + coeff
        return max(0.0, min(100.0, soc))
    except (ValueError, TypeError):
        return 0.0


def _apply_load_correction(base_soc: float, power_w: float) -> float:
    """
    Apply load correction to SOC based on battery power.
    Replicates HA template logic:
    - Discharging (power < 0): voltage sags, actual SOC higher than voltage indicates
    - Charging (power > 0): voltage rises, actual SOC lower than voltage indicates
    - Correction = (power / capacity) * 100 * (1 - efficiency)
    """
    try:
        p = float(power_w)
        soc = float(base_soc)

        if p < 0:  # Discharging
            capacity = BATTERY_CAPACITY_DISCHARGE_AH
            correction = (abs(p) / capacity) * 100.0 * (1.0 - BATTERY_ROUNDTRIP_EFFICIENCY)
            return min(soc + correction, 100.0)
        elif p > 0:  # Charging
            capacity = BATTERY_CAPACITY_CHARGE_AH
            correction = (p / capacity) * 100.0 * (1.0 - BATTERY_ROUNDTRIP_EFFICIENCY)
            return max(soc - correction, 0.0)
        else:
            return soc
    except (ValueError, TypeError):
        return base_soc


def calculate_battery_soc_from_voltage(voltage: float, power_w: float) -> float:
    """
    Calculate corrected battery SOC from voltage and power.
    This is the local replacement for HA's corrected_battery_soc sensor.

    Args:
        voltage: Battery voltage in volts (from D-Bus Dc/Battery/Voltage)
        power_w: Battery power in watts (from D-Bus Dc/Battery/Power,
                 positive=charging, negative=discharging)

    Returns:
        SOC percentage (0-100)
    """
    base_soc = _voltage_to_soc(voltage)
    corrected_soc = _apply_load_correction(base_soc, power_w)
    return round(corrected_soc, 2)


class VictronDBus:
    """
    Fast D-Bus interface for Victron system.
    Uses subprocess calls to dbus-send for maximum speed on Venus OS.
    """

    # Auto-rescan thresholds
    RESCAN_ERROR_THRESHOLD = 5  # Rescan after N consecutive errors
    RESCAN_INTERVAL_SECONDS = 300  # Rescan every 5 minutes regardless

    def __init__(self, test_mode: bool = False):
        self._vebus_service: str | None = None
        self._mppt_services: list = []
        self._consecutive_errors: int = 0
        self._last_scan_time: float = 0
        self._last_success_time: float = 0
        self._dbus_lock = threading.Lock()
        # ESS mode rarely changes; cache it so the per-cycle dashboard read
        # doesn't cost 2 D-Bus roundtrips every loop.
        self._ess_mode_cache: dict[str, Any] | None = None
        self._ess_mode_cache_time: float = 0.0
        # Cache of discovered cell counts per chain service, so we don't probe
        # up to 16 cells every cycle once the real count is known.
        self._chain_cell_counts: dict[str, int] = {}
        # Cache for detailed battery chain cell data (DVCC), refreshed in the
        # background so the control loop never blocks on per-cell dbus calls.
        self._cached_battery_cell_data: dict[str, Any] = {}
        self._last_battery_cell_data_time: float = 0.0
        # Cache for MPPT data to reduce D-Bus calls
        self._cached_mppt_data: dict[str, dict[str, float]] = {}
        self._last_mppt_time: float = 0.0
        # Cache for Tasmota PV power
        self._cached_tasmota_powers: list = []
        self._last_tasmota_time: float = 0.0
        # Cache for battery chain SoC
        self._cached_battery_chain_socs: list = []
        self._last_battery_chain_soc_time: float = 0.0
        # Cache for inverter state
        self._cached_inverter_state: tuple[int, str] = (0, "Unknown")
        self._last_inverter_state_time: float = 0.0
        # Cache for acload (Emporia Vue) power channels
        self._acload_services: list = []
        self._cached_acload_powers: dict[str, float] = {}
        self._last_acload_time: float = 0.0

        self._test_mode = test_mode

        # Background polling thread (like HA does) - skip in test mode
        self._poll_thread: threading.Thread | None = None
        self._poll_stop_event = threading.Event()
        self._poll_interval = 0.2  # Poll at 5Hz, faster than control loop (3Hz)
        self._system_data: dict[str, Any] = {}  # Populated by background polling
        if not test_mode:
            self._start_background_polling()

        self._discover_services()

    def _discover_services(self):
        """Discover VE.Bus, MPPT, and acload services"""

        self._last_scan_time = time.time()
        old_vebus = self._vebus_service

        try:
            result = subprocess.run(
                ["dbus", "-y"], capture_output=True, text=True, timeout=2, check=False
            )
            lines = result.stdout.strip().split("\n")

            self._vebus_service = None
            self._mppt_services = []
            self._acload_services = []

            for line in lines:
                if "com.victronenergy.vebus" in line:
                    self._vebus_service = line.strip()
                elif "com.victronenergy.solarcharger" in line:
                    self._mppt_services.append(line.strip())
                elif "com.victronenergy.acload" in line:
                    self._acload_services.append(line.strip())

            self._mppt_services.sort()
            self._acload_services.sort()

            # Log if service changed
            if old_vebus and self._vebus_service and old_vebus != self._vebus_service:
                print(f"  [D-Bus] VE.Bus service changed: {old_vebus} -> {self._vebus_service}")
            elif not old_vebus and self._vebus_service:
                print(f"  [D-Bus] VE.Bus service found: {self._vebus_service}")

            if self._acload_services:
                print(f"  [D-Bus] acload services found: {len(self._acload_services)}")

            self._consecutive_errors = 0

        except Exception as e:
            logger.debug("D-Bus service discovery failed: %s", e)

    def _check_rescan_needed(self) -> bool:
        """Check if D-Bus rescan is needed and perform it if so"""

        now = time.time()

        # Rescan if too many consecutive errors
        if self._consecutive_errors >= self.RESCAN_ERROR_THRESHOLD:
            print(f"  [D-Bus] Rescanning after {self._consecutive_errors} errors...")
            self._discover_services()
            return True

        # Periodic rescan
        if now - self._last_scan_time > self.RESCAN_INTERVAL_SECONDS:
            self._discover_services()
            return True

        return False

    def _start_background_polling(self):
        """Start background polling thread to keep D-Bus data fresh"""
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return

        self._poll_stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="VictronDBusPoll"
        )
        self._poll_thread.start()
        logger.debug("Background D-Bus polling started")

    def _poll_loop(self):
        """Background polling loop - fetches all D-Bus data periodically"""
        while not self._poll_stop_event.is_set():
            start = time.monotonic()
            try:
                self._poll_all()
            except Exception as e:
                logger.debug("Background poll error: %s", e)

            elapsed = time.monotonic() - start
            sleep_time = max(0.0, self._poll_interval - elapsed)
            if sleep_time > 0:
                self._poll_stop_event.wait(sleep_time)

    def _poll_all(self):
        """Poll all D-Bus data in one pass"""

        # Poll system data (tree query)
        self._poll_system_data()

        # Poll MPPT data (tree query per MPPT)
        if self._mppt_services:
            self._poll_mppt_data_tree()

        # Poll Tasmota PV power
        if TASMOTA_DBUS_SERVICES:
            self._poll_tasmota_power()

        # Poll acload (Emporia Vue) power channels
        if self._acload_services:
            self._poll_acload_power()

        # Poll battery chain SoCs
        self._poll_battery_chain_socs()

        # Poll battery chain cell data (throttled to every 30s)
        self._poll_battery_cell_data_tree()

        # Poll inverter state
        self._poll_inverter_state()

        # Poll inverter power
        self._poll_inverter_power()

    def _poll_system_data(self):
        """Poll system data using tree query"""
        output = self._safe_subprocess(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                f"--dest={SYSTEM_SERVICE}",
                "/",
                GET_VALUE_METHOD,
            ],
            timeout=0.5,
        )
        if output:
            self._parse_system_data(output)

    def _parse_system_data(self, output: str):
        """Parse system data from tree query output"""
        # Simplified patterns to avoid regex backtracking
        patterns = {
            "g1": r"Ac/Grid/L1/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
            "g2": r"Ac/Grid/L2/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
            "t1": r"Ac/Consumption/L1/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
            "t2": r"Ac/Consumption/L2/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
            "bv": r"Dc/Battery/Voltage[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)",
            "bc": r"Dc/Battery/Current[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
            "bp": r"Dc/Battery/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)",
            "pv_total": r"Dc/Pv/Power[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)",
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, output)
            if match:
                try:
                    val = float(match.group(1))
                    self._system_data[key] = int(val) if key not in ("bv", "bc") else val
                except (ValueError, TypeError) as e:
                    logger.debug("System data parse failed for %s: %s", key, e)

        self._system_data["gt"] = self._system_data.get("g1", 0) + self._system_data.get("g2", 0)
        self._system_data["tt"] = self._system_data.get("t1", 0) + self._system_data.get("t2", 0)
        self._system_data["_last_update"] = time.time()

    def _poll_mppt_data_tree(self):
        """Poll all MPPT data using tree queries"""
        data = {}
        for i, service in enumerate(self._mppt_services):
            output = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={service}",
                    "/",
                    GET_VALUE_METHOD,
                ],
                timeout=0.5,
            )
            if output:
                data[f"mppt{i}"] = self._parse_mppt_output(output)

        self._cached_mppt_data = data
        self._last_mppt_time = time.time()

    def _parse_mppt_output(self, output: str) -> dict[str, float]:
        """Parse MPPT power and current from tree query output"""
        mppt_data = {"w": 0.0, "a": 0.0}
        # Parse power - simplified regex to avoid backtracking
        match = re.search(r"Yield/Power[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)", output)
        if match:
            try:
                mppt_data["w"] = float(match.group(1))
            except (ValueError, TypeError):
                logger.debug("MPPT power parse failed: %s", match.group(1))
        # Parse current - simplified regex to avoid backtracking
        match = re.search(r"Dc/0/Current[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)", output)
        if match:
            try:
                mppt_data["a"] = float(match.group(1))
            except (ValueError, TypeError):
                logger.debug("MPPT current parse failed: %s", match.group(1))
        return mppt_data

    def _poll_tasmota_power(self):
        """Poll Tasmota PV power"""
        powers = []
        for service in TASMOTA_DBUS_SERVICES:
            output = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={service}",
                    "/",
                    GET_VALUE_METHOD,
                ],
                timeout=0.3,
            )
            if output:
                match = re.search(r"Ac/Power[^\n]*\n[^\n]*variant\s+\S+\s+(\-?[\d.]+)", output)
                if match:
                    try:
                        powers.append(float(match.group(1)))
                    except (ValueError, TypeError):
                        logger.debug("Tasmota power parse failed: %s", match.group(1))
                        powers.append(0.0)
                else:
                    powers.append(0.0)
            else:
                powers.append(0.0)

        self._cached_tasmota_powers = powers
        self._last_tasmota_time = time.time()

    def _poll_acload_power(self):
        """Poll Emporia Vue power channels (acload services)"""
        powers = {}
        for service in self._acload_services:
            # Query CustomName
            name_output = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    PRINT_REPLY_LITERAL,
                    f"--dest={service}",
                    "/CustomName",
                    GET_VALUE_METHOD,
                ],
                timeout=0.3,
            )
            if not name_output:
                continue
            name_match = re.search(r"variant\s+(\S.*)", name_output.strip())
            if not name_match:
                continue

            # Query Ac/Power
            power_output = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    PRINT_REPLY_LITERAL,
                    f"--dest={service}",
                    "/Ac/Power",
                    GET_VALUE_METHOD,
                ],
                timeout=0.3,
            )
            if not power_output:
                continue
            power_match = re.search(
                r"(?:double|int32|variant\s+(?:double|int32))\s+([-\d.]+)", power_output
            )
            if not power_match:
                continue

            try:
                name = name_match.group(1).strip()
                power = float(power_match.group(1))
                key = name.lower().replace(" ", "_")
                powers[key] = power
            except (ValueError, TypeError) as e:
                logger.debug("acload parse failed: %s", e)
        self._cached_acload_powers = powers
        self._last_acload_time = time.time()

    def _poll_battery_chain_socs(self):
        """Poll battery chain SoCs"""
        battery_services = [
            "com.victronenergy.battery.mqtt_chain1",
            "com.victronenergy.battery.mqtt_chain2",
        ]
        socs = []
        for service in battery_services:
            output = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={service}",
                    "/",
                    GET_VALUE_METHOD,
                ],
                timeout=0.3,
            )
            if output:
                match = re.search(r"Soc[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)", output)
                if match:
                    try:
                        socs.append(float(match.group(1)))
                    except (ValueError, TypeError):
                        logger.debug("Battery chain SoC parse failed: %s", match.group(1))
                        socs.append(0.0)
                else:
                    socs.append(0.0)
            else:
                socs.append(0.0)

        self._cached_battery_chain_socs = socs
        self._last_battery_chain_soc_time = time.time()

    def _poll_battery_cell_data_tree(self):
        """Poll battery chain cell data via one tree query per chain."""
        if time.time() - self._last_battery_cell_data_time < CELL_DATA_POLL_INTERVAL:
            return

        for service in BATTERY_CELL_SERVICES:
            output = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={service}",
                    "/",
                    GET_VALUE_METHOD,
                ],
                timeout=0.5,
            )
            if not output:
                continue

            self._parse_and_cache_chain_data(service, output)

        self._last_battery_cell_data_time = time.time()

    def _parse_and_cache_chain_data(self, service: str, output: str) -> None:
        """Parse tree query output and cache chain data."""
        chain_voltages = self._parse_chain_voltages(service, output)
        if chain_voltages:
            self._chain_cell_counts[service] = len(chain_voltages)

        chain_temps = self._parse_chain_temps(output)
        chain_soc = self._parse_chain_soc(output)
        allow_c = self._tree_bool(output, "Info/AllowCharge")
        allow_d = self._tree_bool(output, "Info/AllowDischarge")

        result = self._cached_battery_cell_data.setdefault(service, {})
        result["voltages"] = chain_voltages
        result["temps"] = chain_temps
        result["soc"] = chain_soc
        result["allow_charge"] = allow_c
        result["allow_discharge"] = allow_d

    def _parse_chain_voltages(self, service: str, output: str) -> list[float]:
        """Parse cell voltages from tree output, stopping at first gap."""
        chain_voltages = []
        known_count = self._chain_cell_counts.get(service, 16)
        max_cell = min(known_count + 1, 16)

        for i in range(1, max_cell + 1):
            match = re.search(
                rf'string "Cell/{i}/Voltage"[^\n]*\n[^\n]*variant\s+\S+\s+([-0-9.]+)',
                output,
            )
            if not match:
                break
            chain_voltages.append(float(match.group(1)))
        return chain_voltages

    def _parse_chain_temps(self, output: str) -> list[float]:
        """Parse cell temperatures from tree output (may be sparse)."""
        chain_temps = []
        for i in range(1, 17):
            match = re.search(
                rf'string "Cell/{i}/Temperature"[^\n]*\n[^\n]*variant\s+\S+\s+([-0-9.]+)',
                output,
            )
            if match:
                chain_temps.append(float(match.group(1)))
        return chain_temps

    def _parse_chain_soc(self, output: str) -> float | None:
        """Parse chain SoC from tree output."""
        soc_match = re.search(r'string "Soc"[^\n]*\n[^\n]*variant\s+\S+\s+([-0-9.]+)', output)
        return float(soc_match.group(1)) if soc_match else None

    @staticmethod
    def _tree_bool(output: str, path: str) -> bool | None:
        """Parse a 0/1 variant value for a path from a tree query reply."""
        match = re.search(
            rf'string "{re.escape(path)}"[^\n]*\n[^\n]*variant\s+\S+\s+([-0-9.]+)',
            output,
        )
        if not match:
            return None
        try:
            return int(float(match.group(1))) == 1
        except (ValueError, TypeError):
            return None

    def _poll_inverter_state(self):
        """Poll inverter state"""
        if not self._vebus_service:
            self._cached_inverter_state = (0, "Unknown")
            self._last_inverter_state_time = time.time()
            return

        val = self._dbus_get(self._vebus_service, "/State")
        if val:
            try:
                code = int(val)
                result = (code, INVERTER_STATES.get(code, f"? ({code})"))
                self._cached_inverter_state = result
            except (ValueError, TypeError) as e:
                logger.debug("Inverter state parse failed: %s", e)

        self._last_inverter_state_time = time.time()

    def _poll_inverter_power(self):
        """Poll inverter power"""
        if not self._vebus_service:
            return

        val = self._dbus_get(self._vebus_service, "/Devices/0/Ac/Inverter/P")
        if val:
            try:
                # Store in system data for fast access
                self._system_data["inv_power"] = int(float(val))
            except (ValueError, TypeError) as e:
                logger.debug("Inverter power parse failed: %s", e)

    @property
    def vebus_service(self) -> str | None:
        return self._vebus_service

    @property
    def mppt_services(self) -> list:
        return self._mppt_services

    def _safe_subprocess(self, cmd: list, timeout: float = 0.3) -> str | None:
        """Run subprocess with strict timeout and error handling"""
        try:
            # Use start_new_session to be able to kill the whole process group
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                start_new_session=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.strip()
        except subprocess.TimeoutExpired:
            pass  # Timeout is expected sometimes
        except Exception as e:
            logger.debug("D-Bus subprocess failed: %s", e)
        return None

    def _dbus_get(self, service: str, path: str) -> str | None:
        """Get a single value from D-Bus (fast)"""

        with self._dbus_lock:
            # Check if rescan needed before operation
            self._check_rescan_needed()

            result = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    PRINT_REPLY_LITERAL,
                    f"--dest={service}",
                    path,
                    GET_VALUE_METHOD,
                ],
                timeout=0.3,
            )
            if result:
                parts = result.split()
                if parts:
                    self._consecutive_errors = 0
                    self._last_success_time = time.time()
                    return parts[-1]

            # Track error
            self._consecutive_errors += 1
            return None

    def _dbus_set(self, service: str, path: str, value: int, value_type: str = "int16") -> bool:
        """Set a value on D-Bus"""

        with self._dbus_lock:
            self._check_rescan_needed()

            result = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    "--type=method_call",
                    f"--dest={service}",
                    path,
                    "com.victronenergy.BusItem.SetValue",
                    f"variant:{value_type}:{value}",
                ],
                timeout=0.3,
            )
            if result is not None:
                self._consecutive_errors = 0
                self._last_success_time = time.time()
                return True

            self._consecutive_errors += 1
            return False

    def get_system_data(self) -> dict[str, Any]:
        """
        Get all system data - now returns instantly from background-poll cache.
        """
        # Return cached data from background polling
        if self._system_data and time.time() - self._system_data.get("_last_update", 0) < 1.0:
            return dict(self._system_data)

        # Fallback: synchronous call if cache stale (should rarely happen)
        data = {
            "g1": 0,
            "g2": 0,
            "gt": 0,
            "t1": 0,
            "t2": 0,
            "tt": 0,
            "bv": 0.0,
            "bc": 0.0,
            "bp": 0,
            "pv_total": 0,
        }

        output = self._safe_subprocess(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                f"--dest={SYSTEM_SERVICE}",
                "/",
                GET_VALUE_METHOD,
            ],
            timeout=0.5,
        )

        if not output:
            return data

        patterns = {
            "g1": r"Ac/Grid/L1/Power.*?\n.*?variant\s+\S+\s+(\-?[\d.]+)",
            "g2": r"Ac/Grid/L2/Power.*?\n.*?variant\s+\S+\s+(\-?[\d.]+)",
            "t1": r"Ac/Consumption/L1/Power.*?\n.*?variant\s+\S+\s+(\-?[\d.]+)",
            "t2": r"Ac/Consumption/L2/Power.*?\n.*?variant\s+\S+\s+(\-?[\d.]+)",
            "bv": r"Dc/Battery/Voltage.*?\n.*?variant\s+\S+\s+([\d.]+)",
            "bc": r"Dc/Battery/Current.*?\n.*?variant\s+\S+\s+(\-?[\d.]+)",
            "bp": r"Dc/Battery/Power.*?\n.*?variant\s+\S+\s+(\-?[\d.]+)",
            "pv_total": r"Dc/Pv/Power.*?\n.*?variant\s+\S+\s+([\d.]+)",
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, output, re.DOTALL)
            if match:
                try:
                    val = float(match.group(1))
                    data[key] = int(val) if key not in ("bv", "bc") else val
                except (ValueError, TypeError) as e:
                    logger.debug("D-Bus system data parse failed for %s: %s", key, e)

        data["gt"] = data["g1"] + data["g2"]
        data["tt"] = data["t1"] + data["t2"]
        return data

    def get_inverter_state(self) -> tuple[int, str]:
        """Get inverter state code and description - instant from background cache"""
        # Return cached data if fresh
        now = time.time()
        if now - self._last_inverter_state_time < 2.0:  # TTL 2 seconds
            return self._cached_inverter_state

        # Background thread keeps this updated, but fallback to sync call if stale
        if not self._vebus_service:
            return 0, "Unknown"

        val = self._dbus_get(self._vebus_service, "/State")
        if val:
            try:
                code = int(val)
                result = (code, INVERTER_STATES.get(code, f"? ({code})"))
                self._cached_inverter_state = result
                self._last_inverter_state_time = now
                return result
            except (ValueError, TypeError) as e:
                logger.debug("Inverter state parse failed: %s", e)
        result = (0, "Unknown")
        self._cached_inverter_state = result
        self._last_inverter_state_time = now
        return result

    def get_battery_soc_local(self, sys_data: dict[str, Any] | None = None) -> float:
        """
        Calculate battery SOC locally from D-Bus voltage and power.
        Replaces HA corrected_battery_soc sensor for independence.
        Returns SOC percentage (0-100).

        Pass the sys_data dict already fetched by get_system_data() in the
        control loop to avoid an extra full D-Bus tree read every cycle.
        """
        from inverter_control.victron import calculate_battery_soc_from_voltage

        if sys_data is None:
            sys_data = self.get_system_data()
        voltage = sys_data.get("bv", 0.0)
        power = sys_data.get("bp", 0)

        return calculate_battery_soc_from_voltage(voltage, power)

    def get_inverter_power(self) -> int:
        """Get current inverter AC output power - instant from background cache"""
        # Background polling keeps _system_data["inv_power"] updated
        return self._system_data.get("inv_power", 0)

    def get_ac_in_power(self) -> int:
        """Get AC input power (from grid) - from system data cache"""
        # Grid power is in system data as gt (grid total)
        return self._system_data.get("gt", 0)

    def set_grid_setpoint(self, watts: int) -> bool:
        """Set the grid power setpoint (Hub4/L1/AcPowerSetpoint)"""
        if not self._vebus_service:
            return False

        return self._dbus_set(self._vebus_service, "/Hub4/L1/AcPowerSetpoint", watts, "int16")

    def get_mppt_data(self) -> dict[str, dict[str, float]]:
        """Get power and current from all MPPT chargers - instant from background cache"""
        # Return cached data if fresh (background poll updates every 0.2s)
        now = time.time()
        if now - self._last_mppt_time < 0.5:  # TTL 0.5 seconds
            return self._cached_mppt_data

        # Background polling keeps this fresh, fallback rarely needed
        return self._cached_mppt_data

    def get_tasmota_pv_power(self) -> list:
        """Get power from Tasmota PV inverters via D-Bus - instant from background cache"""
        # Return cached data if fresh (background poll updates every 0.2s)
        now = time.time()
        if now - self._last_tasmota_time < 0.5:  # TTL 0.5 seconds
            return self._cached_tasmota_powers

        # Background polling keeps this fresh
        return self._cached_tasmota_powers

    def get_acload_powers(self) -> dict[str, float]:
        """Get power from Emporia Vue channels (acload services) - instant from background cache"""
        # Return cached data if fresh (background poll updates every 0.2s)
        now = time.time()
        if now - self._last_acload_time < 0.5:  # TTL 0.5 seconds
            return self._cached_acload_powers

        # Background polling keeps this fresh
        return self._cached_acload_powers

    def get_battery_soc(self) -> float | None:
        """Get battery SoC from system"""
        val = self._dbus_get("com.victronenergy.system", "/Dc/Battery/Soc")
        if val:
            try:
                return float(val)
            except (ValueError, TypeError) as e:
                logger.debug("Battery SoC parse failed: %s", e)
        return None

    def get_battery_chain_socs(self) -> list:
        """Get SoC for each battery chain from D-Bus - instant from background cache

        Returns list of SoC values for:
        - mqtt_chain1 (first series)
        - mqtt_chain2 (second series)
        """
        # Return cached data if fresh (background poll updates every 0.2s)
        now = time.time()
        if now - self._last_battery_chain_soc_time < 2.0:  # TTL 2 seconds
            return self._cached_battery_chain_socs

        # Background polling keeps this fresh
        return self._cached_battery_chain_socs

    def get_ess_mode(self) -> dict[str, Any]:
        """Get current ESS mode

        Returns dict with:
        - hub4_mode: 1=ESS, 3=External control
        - battery_life_state: 0=Optimized without BatteryLife, 1-8=BatteryLife, 9=Keep charged
        - mode_name: Human readable name
        - is_external: True if External control mode
        """
        now = time.time()
        with self._dbus_lock:
            if self._ess_mode_cache is not None and now - self._ess_mode_cache_time < 5.0:
                return dict(self._ess_mode_cache)

        hub4_mode = 0
        bl_state = 0

        val = self._dbus_get(SETTINGS_SERVICE, HUB4_MODE_PATH)
        if val:
            try:
                hub4_mode = int(val)
            except (ValueError, TypeError) as e:
                logger.debug("Hub4 mode parse failed: %s", e)

        val = self._dbus_get(SETTINGS_SERVICE, "/Settings/CGwacs/BatteryLife/State")
        if val:
            try:
                bl_state = int(val)
            except (ValueError, TypeError) as e:
                logger.debug("BatteryLife state parse failed: %s", e)

        # Determine mode name
        # BatteryLife states:
        # 0 or 10 = Optimized without BatteryLife (BatteryLife disabled)
        # 1-8 = Optimized with BatteryLife (various SoC stages)
        # 9 = Keep batteries charged
        if hub4_mode == 3:
            mode_name = "External control"
            is_external = True
        elif hub4_mode == 1:
            is_external = False
            if bl_state in (0, 10):
                mode_name = "Optimized without BatteryLife"
            elif bl_state == 9:
                mode_name = "Keep batteries charged"
            else:
                mode_name = "Optimized (BatteryLife)"
        else:
            mode_name = f"Unknown ({hub4_mode})"
            is_external = False

        result = {
            "hub4_mode": hub4_mode,
            "battery_life_state": bl_state,
            "mode_name": mode_name,
            "is_external": is_external,
        }
        with self._dbus_lock:
            self._ess_mode_cache = result
            self._ess_mode_cache_time = now
        return result

    def set_ess_mode(self, external: bool) -> bool:
        """Set ESS mode

        Args:
            external: True for External control, False for Optimized without BatteryLife

        Returns True if successful
        """
        if external:
            # External control: Hub4Mode = 3
            result = self._dbus_set(SETTINGS_SERVICE, HUB4_MODE_PATH, 3, "int32")
        else:
            # Optimized without BatteryLife: Hub4Mode = 1, BatteryLife/State = 0
            success1 = self._dbus_set(SETTINGS_SERVICE, HUB4_MODE_PATH, 1, "int32")
            success2 = self._dbus_set(
                SETTINGS_SERVICE,
                "/Settings/CGwacs/BatteryLife/State",
                0,
                "int32",
            )
            result = success1 and success2
        # Invalidate the ESS mode cache so the next read is fresh
        with self._dbus_lock:
            self._ess_mode_cache = None
            self._ess_mode_cache_time = 0.0
        return result

    def _get_float(self, service: str, path: str) -> float:
        """Read a D-Bus path and return its float value, or 0.0 on failure."""
        val = self._dbus_get(service, path)
        if val:
            try:
                f = float(val)
                return f if math.isfinite(f) else 0.0
            except (ValueError, TypeError) as e:
                logger.debug("D-Bus float read failed for %s/%s: %s", service, path, e)
        return 0.0

    @staticmethod
    def _battery_state(current: float) -> str:
        if current > 0.5:
            return "Charging"
        if current < -0.5:
            return "Discharging"
        return "Idle"

    @staticmethod
    def _format_time_to_go(ttg_sec: int, state: str) -> str:
        max_reasonable = 86400 * 14
        if state not in ("Charging", "Discharging") or not 0 < ttg_sec < max_reasonable:
            return ""
        h, m = divmod(ttg_sec, 3600)
        m = m // 60
        return f"{h}h {m:02d}m" if h > 0 else f"{m}m"

    def get_all_batteries(self) -> list:
        """Get detailed data for all battery chains including SmartShunt

        Returns list of dicts with: name, voltage, current, power, soc, state,
        time_to_go (formatted), time_to_go_sec (optional).
        """
        battery_services = [
            ("com.victronenergy.battery.dbus-mqtt-chain1", "JBD Chain 1"),
            ("com.victronenergy.battery.dbus-mqtt-chain2", "JBD Chain 2"),
            ("com.victronenergy.battery.virtual_chain", "Virtual Battery"),
        ]

        batteries = []
        for service, name in battery_services:
            current = self._get_float(service, DC_CURRENT_PATH)
            state = self._battery_state(current)

            ttg_sec = None
            ttg_raw = self._dbus_get(service, "/TimeToGo")
            if ttg_raw is not None:
                try:
                    ttg_sec = max(0, int(float(ttg_raw)))
                except (TypeError, ValueError):
                    pass

            batteries.append(
                {
                    "name": name,
                    "voltage": self._get_float(service, "/Dc/0/Voltage"),
                    "current": current,
                    "power": self._get_float(service, "/Dc/0/Power"),
                    "soc": self._get_float(service, "/Soc"),
                    "state": state,
                    "time_to_go": self._format_time_to_go(ttg_sec or 0, state),
                    "time_to_go_sec": ttg_sec,
                }
            )

        return batteries

    def _read_chain_cell_voltages(self, service: str, offset: int) -> list[tuple[float, int]]:
        """Probe cell voltage paths for a chain, updating the cached cell count.

        Once we know how many cells a chain actually reports, stop probing all
        16 possible slots every cycle - just query the known-present cells
        (plus one extra to detect growth). `offset` is the number of voltages
        already collected from other chains, used to build a global cell id.
        """
        known_count = self._chain_cell_counts.get(service, 16)
        max_cell_index = min(known_count + 1, 16)
        discovered_count = 0
        voltages = []

        for i in range(1, max_cell_index + 1):
            val = self._dbus_get(service, f"/Cell/{i}/Voltage")
            if val is None:
                break
            discovered_count = i
            try:
                v = float(val)
                if v > 0:
                    voltages.append((v, offset + len(voltages)))
            except (ValueError, TypeError):
                pass  # Ignore invalid D-Bus values

        # Only grow immediately; keep prior count on a transient miss so a
        # single failed probe doesn't slow re-discovery of all cells.
        if discovered_count > 0:
            self._chain_cell_counts[service] = discovered_count

        return voltages

    def _read_chain_cell_temps(self, service: str) -> list[float]:
        """Probe cell temperature paths for a chain (may be sparse / non-contiguous)."""
        temps = []
        for i in range(1, 17):
            val = self._dbus_get(service, f"/Cell/{i}/Temperature")
            if val is None:
                continue
            try:
                t = float(val)
                if -50 <= t <= 100:  # Sanity check
                    temps.append(t)
            except (ValueError, TypeError):
                pass  # Ignore invalid temperature values
        return temps

    def _read_chain_soc(self, service: str) -> float | None:
        soc_val = self._dbus_get(service, "/Soc")
        if soc_val is None:
            return None
        try:
            return float(soc_val)
        except (ValueError, TypeError):
            return None  # Ignore invalid SoC value

    def _read_chain_allow_flag(self, service: str, path: str) -> bool | None:
        val = self._dbus_get(service, path)
        if val is None:
            return None
        try:
            return int(float(val)) == 1
        except (ValueError, TypeError):
            return None  # Ignore invalid allow-flag value

    def get_battery_cell_data(self) -> dict[str, Any]:
        """Get detailed cell data from battery chains for DVCC calculation.

        Returns dict with:
        - max_cell, max_cell_id, min_cell, min_cell_id: Cell voltage extremes
        - max_temp, min_temp: Temperature extremes
        - soc: Overall SoC
        - allow_charge, allow_discharge: BMS flags
        """
        self._maybe_refresh_cell_cache()
        cached = self._build_from_cache()
        if cached:
            return cached
        return self._get_battery_cell_data_live()

    def _maybe_refresh_cell_cache(self) -> None:
        """Refresh cache if stale (called before reading)."""
        if time.time() - self._last_battery_cell_data_time >= CELL_DATA_POLL_INTERVAL:
            self._poll_battery_cell_data_tree()

    def _build_from_cache(self) -> dict[str, Any] | None:
        """Build result from cached data. Returns None if cache empty/invalid."""
        if not self._cached_battery_cell_data:
            return None

        voltages, temps, total_soc, soc_count, allow_charge, allow_discharge = (
            self._aggregate_cached_chains()
        )

        if not (voltages or temps or soc_count):
            return None

        return self._build_cell_result(
            voltages, temps, total_soc, soc_count, allow_charge, allow_discharge
        )

    def _aggregate_cached_chains(self) -> tuple:
        """Aggregate data from all cached battery chains."""
        voltages: list[tuple[float, int]] = []
        temps: list[float] = []
        total_soc = 0.0
        soc_count = 0
        allow_charge = True
        allow_discharge = True

        for service in BATTERY_CELL_SERVICES:
            entry = self._cached_battery_cell_data.get(service)
            if not entry:
                continue
            voltages, temps, total_soc, soc_count, allow_charge, allow_discharge = (
                self._accumulate_chain_data(
                    entry, voltages, temps, total_soc, soc_count, allow_charge, allow_discharge
                )
            )

        return voltages, temps, total_soc, soc_count, allow_charge, allow_discharge

    def _accumulate_chain_data(
        self,
        entry: dict,
        voltages: list,
        temps: list,
        total_soc: float,
        soc_count: int,
        allow_charge: bool,
        allow_discharge: bool,
    ) -> tuple:
        """Accumulate single chain's data into aggregate."""
        for v in entry.get("voltages", []):
            if v > 0:
                voltages.append((v, len(voltages)))
        temps.extend(entry.get("temps", []))

        soc = entry.get("soc")
        if soc is not None:
            total_soc += soc
            soc_count += 1

        if entry.get("allow_charge") is not None:
            allow_charge = allow_charge and entry["allow_charge"]
        if entry.get("allow_discharge") is not None:
            allow_discharge = allow_discharge and entry["allow_discharge"]

        return voltages, temps, total_soc, soc_count, allow_charge, allow_discharge

    def _get_battery_cell_data_live(self) -> dict[str, Any]:
        """Legacy live per-path D-Bus read (fallback when cache is stale)."""
        return self._build_cell_result(*self._collect_live_cell_data())

    def _collect_live_cell_data(self) -> tuple:
        """Collect all live cell data from battery chains."""
        all_cell_voltages: list[tuple[float, int]] = []
        all_cell_temps: list[float] = []
        total_soc = 0.0
        soc_count = 0
        allow_charge = True
        allow_discharge = True

        for service in BATTERY_CELL_SERVICES:
            all_cell_voltages.extend(
                self._read_chain_cell_voltages(service, len(all_cell_voltages))
            )
            all_cell_temps.extend(self._read_chain_cell_temps(service))

            total_soc, soc_count, allow_charge, allow_discharge = self._accumulate_live_chain_data(
                service, total_soc, soc_count, allow_charge, allow_discharge
            )

        return (
            all_cell_voltages,
            all_cell_temps,
            total_soc,
            soc_count,
            allow_charge,
            allow_discharge,
        )

    def _accumulate_live_chain_data(
        self,
        service: str,
        total_soc: float,
        soc_count: int,
        allow_charge: bool,
        allow_discharge: bool,
    ) -> tuple:
        """Accumulate live chain SOC and allow flags."""
        soc = self._read_chain_soc(service)
        if soc is not None:
            total_soc += soc
            soc_count += 1

        allow_c = self._read_chain_allow_flag(service, "/Info/AllowCharge")
        if allow_c is not None:
            allow_charge = allow_charge and allow_c

        allow_d = self._read_chain_allow_flag(service, "/Info/AllowDischarge")
        if allow_d is not None:
            allow_discharge = allow_discharge and allow_d

        return total_soc, soc_count, allow_charge, allow_discharge

    @staticmethod
    def _build_cell_result(
        all_cell_voltages: list[tuple[float, int]],
        all_cell_temps: list[float],
        total_soc: float,
        soc_count: int,
        allow_charge: bool,
        allow_discharge: bool,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "max_cell": None,
            "max_cell_id": None,
            "min_cell": None,
            "min_cell_id": None,
            "max_temp": None,
            "min_temp": None,
            "soc": round(total_soc / soc_count, 1) if soc_count else None,
            "allow_charge": allow_charge,
            "allow_discharge": allow_discharge,
        }

        if all_cell_voltages:
            all_cell_voltages.sort(key=lambda x: x[0], reverse=True)
            result["max_cell"] = round(all_cell_voltages[0][0], 3)
            result["max_cell_id"] = all_cell_voltages[0][1]
            result["min_cell"] = round(all_cell_voltages[-1][0], 3)
            result["min_cell_id"] = all_cell_voltages[-1][1]

        if all_cell_temps:
            result["max_temp"] = round(max(all_cell_temps), 1)
            result["min_temp"] = round(min(all_cell_temps), 1)

        return result

    def get_mppt_chargers(self) -> list:
        """Get detailed data for all MPPT chargers

        Returns list of dicts with: name, pv_voltage, current, power
        """
        chargers = []
        for i, service in enumerate(self._mppt_services):
            parts = service.split(":")
            name = f"MPPT-{parts[1]}" if len(parts) > 1 else f"MPPT-{i}"
            chargers.append(
                {
                    "name": name,
                    "pv_voltage": self._get_float(service, "/Pv/V"),
                    "current": self._get_float(service, DC_CURRENT_PATH),
                    "power": self._get_float(service, "/Yield/Power"),
                }
            )
        return chargers

    def get_mppt_daily_yields(self) -> list[float]:
        """Get daily yield (kWh) for each MPPT charger from D-Bus"""
        yields = []
        for service in self._mppt_services:
            # /History/Daily/0/Yield = today's yield in kWh
            yield_kwh = self._get_float(service, "/History/Daily/0/Yield")
            yields.append(yield_kwh)
        return yields

    def get_pv_inverter_daily_yields(self) -> list[float]:
        """Get daily yield (kWh) for each Tasmota PV inverter from D-Bus.

        Tasmota inverters only expose lifetime energy (/Ac/Energy/Forward).
        We track daily yield by storing the lifetime value at midnight and
        computing the difference.
        """
        yields = []
        for i, service in enumerate(TASMOTA_DBUS_SERVICES):
            lifetime_kwh = self._get_float(service, "/Ac/Energy/Forward")
            if lifetime_kwh <= 0:
                yields.append(0.0)
                continue

            # Initialize midnight tracker on first use
            if not hasattr(self, "_tasmota_midnight_kwh"):
                self._tasmota_midnight_kwh = [
                    self._get_float(s, "/Ac/Energy/Forward") for s in TASMOTA_DBUS_SERVICES
                ]
                self._tasmota_midnight_date = time.localtime().tm_yday

            # Check if date changed (new day)
            today = time.localtime().tm_yday
            if today != self._tasmota_midnight_date:
                # New day - update midnight reference to current lifetime
                self._tasmota_midnight_kwh = [
                    self._get_float(s, "/Ac/Energy/Forward") for s in TASMOTA_DBUS_SERVICES
                ]
                self._tasmota_midnight_date = today

            # Daily yield = current lifetime - lifetime at midnight
            daily_yield = lifetime_kwh - self._tasmota_midnight_kwh[i]
            yields.append(max(0.0, daily_yield))
        return yields

    def get_battery_daily_energy(self) -> tuple[float, float]:
        """Get battery daily charge/discharge energy (kWh) from system D-Bus

        Returns (charge_kwh, discharge_kwh)
        """
        charge = self._get_float(SYSTEM_SERVICE, "/History/Daily/0/ChargeEnergy")
        discharge = self._get_float(SYSTEM_SERVICE, "/History/Daily/0/DischargeEnergy")
        return charge, discharge

    def get_total_solar_yield_today(self) -> float:
        """Get total solar production today (kWh) from all sources"""
        mppt_yields = self.get_mppt_daily_yields()
        pv_yields = self.get_pv_inverter_daily_yields()
        return sum(mppt_yields) + sum(pv_yields)


# Singleton instance
_victron: VictronDBus | None = None


def get_victron(test_mode: bool = False) -> VictronDBus:
    """Get or create Victron D-Bus interface"""
    global _victron  # pylint: disable=global-statement
    if _victron is None:
        _victron = VictronDBus(test_mode=test_mode)
    return _victron


def reset_victron_for_testing() -> None:
    """Reset singleton for testing purposes"""
    global _victron  # pylint: disable=global-statement
    if _victron is not None:
        _victron._poll_stop_event.set()
        if _victron._poll_thread:
            _victron._poll_thread.join(timeout=1.0)
    _victron = None

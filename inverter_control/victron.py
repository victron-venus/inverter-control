#!/usr/bin/env python3
"""
Victron D-Bus Interface
Fast D-Bus access for grid control and monitoring
"""

import logging
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


class VictronDBus:
    """
    Fast D-Bus interface for Victron system.
    Uses subprocess calls to dbus-send for maximum speed on Venus OS.
    """

    # Auto-rescan thresholds
    RESCAN_ERROR_THRESHOLD = 5  # Rescan after N consecutive errors
    RESCAN_INTERVAL_SECONDS = 300  # Rescan every 5 minutes regardless

    def __init__(self):
        self._vebus_service: str | None = None
        self._mppt_services: list = []
        self._consecutive_errors: int = 0
        self._last_scan_time: float = 0
        self._last_success_time: float = 0
        self._dbus_lock = threading.Lock()
        self._discover_services()

    def _discover_services(self):
        """Discover VE.Bus and MPPT services"""

        self._last_scan_time = time.time()
        old_vebus = self._vebus_service

        try:
            result = subprocess.run(
                ["dbus", "-y"], capture_output=True, text=True, timeout=2, check=False
            )
            lines = result.stdout.strip().split("\n")

            self._vebus_service = None
            self._mppt_services = []

            for line in lines:
                if "com.victronenergy.vebus" in line:
                    self._vebus_service = line.strip()
                elif "com.victronenergy.solarcharger" in line:
                    self._mppt_services.append(line.strip())

            self._mppt_services.sort()

            # Log if service changed
            if old_vebus and self._vebus_service and old_vebus != self._vebus_service:
                print(f"  [D-Bus] VE.Bus service changed: {old_vebus} -> {self._vebus_service}")
            elif not old_vebus and self._vebus_service:
                print(f"  [D-Bus] VE.Bus service found: {self._vebus_service}")

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
                    "--print-reply=literal",
                    f"--dest={service}",
                    path,
                    "com.victronenergy.BusItem.GetValue",
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
        Get all system data in one D-Bus call (fastest method).
        Returns dict with grid, consumption, battery, and solar data.
        """
        data = {
            "g1": 0,
            "g2": 0,
            "gt": 0,  # Grid L1, L2, Total
            "t1": 0,
            "t2": 0,
            "tt": 0,  # Consumption L1, L2, Total
            "bv": 0.0,  # Battery voltage
            "bc": 0.0,  # Battery current
            "bp": 0,  # Battery power
            "pv_total": 0,  # Total PV power
        }

        output = self._safe_subprocess(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                "--dest=com.victronenergy.system",
                "/",
                "com.victronenergy.BusItem.GetValue",
            ],
            timeout=0.5,
        )

        if not output:
            return data

        # Parse with regex for speed
        patterns = {
            "g1": r"Ac/Grid/L1/Power.*?\n.*?\s(\-?[\d.]+)",
            "g2": r"Ac/Grid/L2/Power.*?\n.*?\s(\-?[\d.]+)",
            "t1": r"Ac/Consumption/L1/Power.*?\n.*?\s(\-?[\d.]+)",
            "t2": r"Ac/Consumption/L2/Power.*?\n.*?\s(\-?[\d.]+)",
            "bv": r"Dc/Battery/Voltage.*?\n.*?\s([\d.]+)",
            "bc": r"Dc/Battery/Current.*?\n.*?\s(\-?[\d.]+)",
            "bp": r"Dc/Battery/Power.*?\n.*?\s(\-?[\d.]+)",
            "pv_total": r"Dc/Pv/Power.*?\n.*?\s([\d.]+)",
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
        """Get inverter state code and description"""
        if not self._vebus_service:
            return 0, "Unknown"

        val = self._dbus_get(self._vebus_service, "/State")
        if val:
            try:
                code = int(val)
                return code, INVERTER_STATES.get(code, f"? ({code})")
            except (ValueError, TypeError) as e:
                logger.debug("Inverter state parse failed: %s", e)
        return 0, "Unknown"

    def get_inverter_power(self) -> int:
        """Get current inverter AC output power"""
        if not self._vebus_service:
            return 0

        # Try specific device path first (faster)
        val = self._dbus_get(self._vebus_service, "/Devices/0/Ac/Inverter/P")
        if val:
            try:
                return int(float(val))
            except (ValueError, TypeError) as e:
                logger.debug("Inverter power parse failed: %s", e)
        return 0

    def get_ac_in_power(self) -> int:
        """Get AC input power (from grid)"""
        if not self._vebus_service:
            return 0

        val = self._dbus_get(self._vebus_service, "/Ac/ActiveIn/L1/P")
        if val:
            try:
                return int(float(val))
            except (ValueError, TypeError) as e:
                logger.debug("AC input power parse failed: %s", e)
        return 0

    def set_grid_setpoint(self, watts: int) -> bool:
        """Set the grid power setpoint (Hub4/L1/AcPowerSetpoint)"""
        if not self._vebus_service:
            return False

        return self._dbus_set(self._vebus_service, "/Hub4/L1/AcPowerSetpoint", watts, "int16")

    def get_mppt_data(self) -> dict[str, dict[str, float]]:
        """Get power and current from all MPPT chargers"""
        data = {}

        for i, service in enumerate(self._mppt_services):
            mppt_data = {"w": 0.0, "a": 0.0}

            # Get power
            val = self._dbus_get(service, "/Yield/Power")
            if val:
                try:
                    mppt_data["w"] = float(val)
                except (ValueError, TypeError) as e:
                    logger.debug("MPPT power parse failed for %s: %s", service, e)

            # Get current
            val = self._dbus_get(service, DC_CURRENT_PATH)
            if val:
                try:
                    mppt_data["a"] = float(val)
                except (ValueError, TypeError) as e:
                    logger.debug("MPPT current parse failed for %s: %s", service, e)

            data[f"mppt{i}"] = mppt_data

        return data

    def get_tasmota_pv_power(self) -> list:
        """Get power from Tasmota PV inverters via D-Bus"""
        powers = []

        for service in TASMOTA_DBUS_SERVICES:
            val = self._dbus_get(service, "/Ac/Power")
            if val:
                try:
                    powers.append(float(val))
                except (ValueError, TypeError) as e:
                    logger.debug("Tasmota PV power parse failed for %s: %s", service, e)
                    powers.append(0.0)
            else:
                powers.append(0.0)

        return powers

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
        """Get SoC for each battery chain from D-Bus

        Returns list of SoC values for:
        - mqtt_chain1 (first series)
        - mqtt_chain2 (second series)
        """
        battery_services = [
            "com.victronenergy.battery.mqtt_chain1",
            "com.victronenergy.battery.mqtt_chain2",
        ]

        socs = []
        for service in battery_services:
            val = self._dbus_get(service, "/Soc")
            if val:
                try:
                    socs.append(float(val))
                except (ValueError, TypeError) as e:
                    logger.debug("Battery chain SoC parse failed for %s: %s", service, e)
                    socs.append(0.0)
            else:
                socs.append(0.0)

        return socs

    def get_ess_mode(self) -> dict[str, Any]:
        """Get current ESS mode

        Returns dict with:
        - hub4_mode: 1=ESS, 3=External control
        - battery_life_state: 0=Optimized without BatteryLife, 1-8=BatteryLife, 9=Keep charged
        - mode_name: Human readable name
        - is_external: True if External control mode
        """
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

        return {
            "hub4_mode": hub4_mode,
            "battery_life_state": bl_state,
            "mode_name": mode_name,
            "is_external": is_external,
        }

    def set_ess_mode(self, external: bool) -> bool:
        """Set ESS mode

        Args:
            external: True for External control, False for Optimized without BatteryLife

        Returns True if successful
        """
        if external:
            # External control: Hub4Mode = 3
            return self._dbus_set(SETTINGS_SERVICE, HUB4_MODE_PATH, 3, "int32")
        # Optimized without BatteryLife: Hub4Mode = 1, BatteryLife/State = 0
        success1 = self._dbus_set(SETTINGS_SERVICE, HUB4_MODE_PATH, 1, "int32")
        success2 = self._dbus_set(
            SETTINGS_SERVICE,
            "/Settings/CGwacs/BatteryLife/State",
            0,
            "int32",
        )
        return success1 and success2

    def _get_float(self, service: str, path: str) -> float:
        """Read a D-Bus path and return its float value, or 0.0 on failure."""
        val = self._dbus_get(service, path)
        if val:
            try:
                return float(val)
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

    def get_battery_cell_data(self) -> dict[str, Any]:
        """Get detailed cell data from battery chains for DVCC calculation.

        Returns dict with:
        - max_cell: Highest cell voltage across all chains
        - max_cell_id: Cell index of highest voltage
        - min_cell: Lowest cell voltage across all chains
        - min_cell_id: Cell index of lowest voltage
        - max_temp: Highest cell temperature
        - min_temp: Lowest cell temperature
        - soc: Overall SoC
        - allow_charge: Whether BMS allows charging
        - allow_discharge: Whether BMS allows discharging
        """
        cell_services = [
            "com.victronenergy.battery.dbus-mqtt-chain1",
            "com.victronenergy.battery.dbus-mqtt-chain2",
        ]

        all_cell_voltages = []
        all_cell_temps = []
        total_soc = 0.0
        soc_count = 0
        allow_charge = True
        allow_discharge = True

        for service in cell_services:
            # Get cell voltages - path list of cell voltage paths
            for i in range(1, 17):  # Support up to 16 cells per chain
                path = f"/Cell/{i}/Voltage"
                val = self._dbus_get(service, path)
                if val is not None:
                    try:
                        v = float(val)
                        if v > 0:
                            all_cell_voltages.append((v, len(all_cell_voltages)))
                    except (ValueError, TypeError):
                        pass  # Ignore invalid D-Bus values

            # Get cell temperatures
            for i in range(1, 17):
                path = f"/Cell/{i}/Temperature"
                val = self._dbus_get(service, path)
                if val is not None:
                    try:
                        t = float(val)
                        if -50 <= t <= 100:  # Sanity check
                            all_cell_temps.append(t)
                    except (ValueError, TypeError):
                        pass  # Ignore invalid temperature values

            # Get SoC
            soc_val = self._dbus_get(service, "/Soc")
            if soc_val is not None:
                try:
                    total_soc += float(soc_val)
                    soc_count += 1
                except (ValueError, TypeError):
                    pass  # Ignore invalid SoC values

            # Get BMS allow signals
            allow_c = self._dbus_get(service, "/Info/AllowCharge")
            if allow_c is not None:
                try:
                    allow_charge = allow_charge and (int(float(allow_c)) == 1)
                except (ValueError, TypeError):
                    pass  # Ignore invalid AllowCharge value

            allow_d = self._dbus_get(service, "/Info/AllowDischarge")
            if allow_d is not None:
                try:
                    allow_discharge = allow_discharge and (int(float(allow_d)) == 1)
                except (ValueError, TypeError):
                    pass  # Ignore invalid AllowDischarge value

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


# Singleton instance
_victron: VictronDBus | None = None


def get_victron() -> VictronDBus:
    """Get or create Victron D-Bus interface"""
    global _victron  # pylint: disable=global-statement
    if _victron is None:
        _victron = VictronDBus()
    return _victron

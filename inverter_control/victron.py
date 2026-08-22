#!/usr/bin/env python3
"""
Victron D-Bus Interface
Fast D-Bus access for grid control and monitoring
"""

import json
import logging
import math
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import INVERTER_STATES
from .victron_parse import (
    VARIANT_RE,
    calculate_battery_soc_from_voltage,
    extract_acload_name_power,
    extract_power_from_tree,
    parse_mppt_output,
    parse_system_data_output,
    parse_variant_value,
)

logger = logging.getLogger("inverter-control")

# D-Bus path constants
DC_CURRENT_PATH = "/Dc/0/Current"
SETTINGS_SERVICE = "com.victronenergy.settings"
HUB4_MODE_PATH = "/Settings/CGwacs/Hub4Mode"
SYSTEM_SERVICE = "com.victronenergy.system"
GET_VALUE_METHOD = "com.victronenergy.BusItem.GetValue"
PRINT_REPLY_LITERAL = "--print-reply=literal"
TASMOTA_ENERGY_FORWARD_PATH = "/Ac/Energy/Forward"
TASMOTA_ENERGY_DAILY_PATH = "/Ac/Energy/Daily"
# Published by dbus-tasmota-pv >= 3.0 (Tasmota ENERGY.Yesterday)
TASMOTA_ENERGY_YESTERDAY_PATH = "/Energy/Daily/Yesterday"
AC_POWER_PATH = "/Ac/Power"
# Battery daily energy is integrated from battery power (no D-Bus history on
# dbus-systemcalc-py based systems). State file survives service restarts.
BATTERY_ENERGY_STATE_FILE = "/data/inverter-control/battery_daily_energy.json"
BATTERY_ENERGY_PERSIST_INTERVAL = 30.0

# Battery chains with per-cell data, polled as full tree queries in the
# background. Reading each Cell/N/Voltage with a separate dbus-send subprocess
# (as get_battery_cell_data used to) is ~72 subprocess calls per cycle which
# blows the 5s cycle watchdog on a slow RPi.
BATTERY_CHAIN_1 = "com.victronenergy.battery.mqtt_chain1"
BATTERY_CHAIN_2 = "com.victronenergy.battery.mqtt_chain2"
BATTERY_CELL_SERVICES = [BATTERY_CHAIN_1, BATTERY_CHAIN_2]
# How often the background poller refreshes the cell-data cache.
CELL_DATA_POLL_INTERVAL = 30


class VictronDBus:
    """
    Fast D-Bus interface for Victron system.
    Uses subprocess calls to dbus-send for maximum speed on Venus OS.
    """

    # Auto-rescan thresholds
    RESCAN_ERROR_THRESHOLD = 5  # Rescan after N consecutive errors
    RESCAN_INTERVAL_SECONDS = 300  # Rescan every 5 minutes regardless
    RESCAN_COOLDOWN_SECONDS = 60  # Minimum time between error-triggered rescans

    # Service health tracking: after N consecutive timeouts, back off
    SERVICE_FAIL_THRESHOLD = 3  # Consecutive failures before backing off
    SERVICE_BACKOFF_BASE = 10.0  # Initial backoff: 10s
    SERVICE_BACKOFF_MAX = 300.0  # Max backoff: 5 minutes
    SERVICE_PROBE_INTERVAL = 30.0  # How often to probe backed-off services

    def __init__(self, test_mode: bool = False):
        self._vebus_service: str | None = None
        self._mppt_services: list = []
        self._consecutive_errors: int = 0
        self._last_scan_time: float = 0
        self._last_success_time: float = 0
        self._last_rescan_time: float = 0  # Cooldown tracker for error-triggered rescans
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
        self._cached_pv_powers: list = []
        self._last_pv_time: float = 0.0
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
        # Cache for discovered Tasmota PV inverter services
        self._pv_inverter_services: list = []
        # Cache for daily/yesterday yields (MPPT + Tasmota) and battery daily energy
        self._cached_mppt_daily_yields: list[float] = []
        self._cached_tasmota_daily_yields: list[float] = []
        self._cached_mppt_yesterday_yields: list[float] = []
        self._cached_tasmota_yesterday_yields: list[float] = []
        self._cached_battery_daily_energy: tuple[float, float] = (0.0, 0.0)
        self._last_daily_yields_time: float = 0.0
        self._last_battery_daily_energy_time: float = 0.0
        # Cache for get_all_batteries() and get_mppt_chargers() - avoid 15+ D-Bus calls per cycle
        self._cached_all_batteries: list = []
        self._last_all_batteries_time: float = 0.0
        self._cached_mppt_chargers: list = []
        self._last_mppt_chargers_time: float = 0.0
        # Battery daily energy integration state (charge/discharge kWh)
        self._battery_energy_file = BATTERY_ENERGY_STATE_FILE
        self._battery_energy_date: int = 0
        self._battery_energy_last_time: float = 0.0
        self._battery_energy_last_persist: float = 0.0
        # Service health tracking: detect unresponsive D-Bus services
        # and back off to avoid 4+ second freezes from sequential timeouts.
        self._service_consecutive_fails: dict[str, int] = {}
        self._service_backoff_until: dict[str, float] = {}
        # Venus OS system clock runs UTC; user timezone lives in localsettings,
        # read lazily on first _local_today() (localsettings may lag at boot).
        self._tz_name: str = ""
        self._load_battery_daily_energy()

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
        """Discover VE.Bus, MPPT, acload, and Tasmota PV inverter services"""

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
            self._pv_inverter_services = []

            for line in lines:
                if "com.victronenergy.vebus" in line:
                    self._vebus_service = line.strip()
                elif "com.victronenergy.solarcharger" in line:
                    self._mppt_services.append(line.strip())
                elif "com.victronenergy.acload" in line:
                    self._acload_services.append(line.strip())
                elif "com.victronenergy.pvinverter." in line:
                    self._pv_inverter_services.append(line.strip())

            self._mppt_services.sort()
            self._acload_services.sort()
            self._pv_inverter_services.sort()

            # Log if service changed
            if old_vebus and self._vebus_service and old_vebus != self._vebus_service:
                print(f"  [D-Bus] VE.Bus service changed: {old_vebus} -> {self._vebus_service}")
            elif not old_vebus and self._vebus_service:
                print(f"  [D-Bus] VE.Bus service found: {self._vebus_service}")

            if self._acload_services:
                print(f"  [D-Bus] acload services found: {len(self._acload_services)}")

            if self._pv_inverter_services:
                print(f"  [D-Bus] PV inverters found: {self._pv_inverter_services}")

            self._consecutive_errors = 0

        except Exception as e:
            logger.debug("D-Bus service discovery failed: %s", e)

    def _check_rescan_needed(self) -> bool:
        """Check if D-Bus rescan is needed and perform it if so.
        IMPORTANT: This must NOT be called while holding _dbus_lock,
        as _discover_services() runs a blocking subprocess (dbus -y)
        that would prevent the main control loop from acquiring the lock."""

        now = time.time()

        # Rescan if too many consecutive errors (with cooldown to prevent storm)
        if self._consecutive_errors >= self.RESCAN_ERROR_THRESHOLD:
            if now - self._last_rescan_time < self.RESCAN_COOLDOWN_SECONDS:
                return False  # Too soon for another error-triggered rescan
            print(f"  [D-Bus] Rescanning after {self._consecutive_errors} errors...")
            self._last_rescan_time = now
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
                # Check for rescan OUTSIDE the lock to avoid blocking main thread
                self._check_rescan_needed()
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
        if self._pv_inverter_services:
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

        # Poll daily yields and battery energy (throttled to every 5s)
        self._poll_daily_yields()
        self._poll_battery_daily_energy()

    def _poll_system_data(self):
        """Poll system data using tree query"""
        output = self._safe_subprocess_tracked(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                f"--dest={SYSTEM_SERVICE}",
                "/",
                GET_VALUE_METHOD,
            ],
            service=SYSTEM_SERVICE,
            timeout=0.5,
        )
        if output:
            self._parse_system_data(output)

    def _parse_system_data(self, output: str):
        """Parse system data from tree query output using shared parser"""
        parsed = parse_system_data_output(output)
        self._system_data.update(parsed)
        self._system_data["_last_update"] = time.time()

    def _poll_mppt_data_tree(self):
        """Poll all MPPT data using parallel tree queries"""
        data = {}

        def _query_mppt(idx: int, service: str) -> tuple[int, dict[str, float]]:
            output = self._safe_subprocess_tracked(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={service}",
                    "/",
                    GET_VALUE_METHOD,
                ],
                service=service,
                timeout=0.5,
            )
            return idx, parse_mppt_output(output) if output else {"w": 0.0, "a": 0.0}

        with ThreadPoolExecutor(max_workers=len(self._mppt_services)) as pool:
            futures = [
                pool.submit(_query_mppt, i, svc) for i, svc in enumerate(self._mppt_services)
            ]
            for future in as_completed(futures):
                idx, mppt_data = future.result()
                data[f"mppt{idx}"] = mppt_data

        self._cached_mppt_data = data
        self._last_mppt_time = time.time()

    def _query_pv_powers(self, path: str = "/", reply_mode: str = "--print-reply") -> list:
        """Query all PV inverter services for power (shared by poll and fallback)."""
        powers = []
        for service in self._pv_inverter_services:
            output = self._safe_subprocess_tracked(
                [
                    "dbus-send",
                    "--system",
                    reply_mode,
                    f"--dest={service}",
                    path,
                    GET_VALUE_METHOD,
                ],
                service=service,
                timeout=0.3,
            )
            powers.append(extract_power_from_tree(output))
        return powers

    def _poll_tasmota_power(self):
        """Poll Tasmota PV power"""
        self._cached_pv_powers = self._query_pv_powers()
        self._last_pv_time = time.time()

    def _query_acload_powers(self) -> dict[str, float]:
        """Query all acload services for name+power (shared by poll and fallback)."""
        powers = {}
        for service in self._acload_services:
            name_output = self._safe_subprocess_tracked(
                [
                    "dbus-send",
                    "--system",
                    PRINT_REPLY_LITERAL,
                    f"--dest={service}",
                    "/CustomName",
                    GET_VALUE_METHOD,
                ],
                service=service,
                timeout=0.3,
            )
            power_output = self._safe_subprocess_tracked(
                [
                    "dbus-send",
                    "--system",
                    PRINT_REPLY_LITERAL,
                    f"--dest={service}",
                    AC_POWER_PATH,
                    GET_VALUE_METHOD,
                ],
                service=service,
                timeout=0.3,
            )
            result = extract_acload_name_power((name_output, power_output))
            if result:
                key, power = result
                powers[key] = power
        return powers

    def _poll_acload_power(self):
        """Poll Emporia Vue power channels (acload services)"""
        self._cached_acload_powers = self._query_acload_powers()
        self._last_acload_time = time.time()

    def _query_battery_chain_socs(
        self,
        path: str = "/",
        reply_mode: str = "--print-reply",
        soc_regex: re.Pattern | None = None,
    ) -> list[float]:
        """Query all battery chain services for SoC (shared by poll and fallback)."""
        if soc_regex is None:
            soc_regex = re.compile(r"Soc[^\n]*\n[^\n]*variant\s+\S+\s+([\d.]+)")
        battery_services = [
            BATTERY_CHAIN_1,
            "com.victronenergy.battery.mqtt_chain2",
        ]
        socs = []
        for service in battery_services:
            output = self._safe_subprocess_tracked(
                [
                    "dbus-send",
                    "--system",
                    reply_mode,
                    f"--dest={service}",
                    path,
                    GET_VALUE_METHOD,
                ],
                service=service,
                timeout=0.3,
            )
            if output:
                match = soc_regex.search(output)
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
        return socs

    def _poll_battery_chain_socs(self):
        """Poll battery chain SoCs"""
        self._cached_battery_chain_socs = self._query_battery_chain_socs()
        self._last_battery_chain_soc_time = time.time()

    def _poll_battery_cell_data_tree(self):
        """Poll battery chain cell data via one tree query per chain."""
        if time.time() - self._last_battery_cell_data_time < CELL_DATA_POLL_INTERVAL:
            return

        for service in BATTERY_CELL_SERVICES:
            output = self._safe_subprocess_tracked(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={service}",
                    "/",
                    GET_VALUE_METHOD,
                ],
                service=service,
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

    @staticmethod
    def _parse_inverter_state_code(raw: str) -> tuple[int, str]:
        """Parse inverter state code from raw D-Bus output."""
        code = int(raw.strip())
        return code, INVERTER_STATES.get(code, f"? ({code})")

    def _poll_inverter_state(self):
        """Poll inverter state (uses _safe_subprocess directly to avoid lock contention)"""
        if not self._vebus_service:
            self._cached_inverter_state = (0, "Unknown")
            self._last_inverter_state_time = time.time()
            return

        output = self._safe_subprocess_tracked(
            [
                "dbus-send",
                "--system",
                PRINT_REPLY_LITERAL,
                f"--dest={self._vebus_service}",
                "/State",
                GET_VALUE_METHOD,
            ],
            service=self._vebus_service,
            timeout=0.3,
        )
        if output:
            try:
                result = self._parse_inverter_state_code(output)
                self._cached_inverter_state = result
                self._consecutive_errors = 0
            except (ValueError, TypeError) as e:
                logger.debug("Inverter state parse failed: %s", e)
                self._consecutive_errors += 1
        else:
            self._consecutive_errors += 1

        self._last_inverter_state_time = time.time()

    def _poll_inverter_power(self):
        """Poll inverter power (uses _safe_subprocess directly to avoid lock contention)"""
        if not self._vebus_service:
            return

        output = self._safe_subprocess_tracked(
            [
                "dbus-send",
                "--system",
                PRINT_REPLY_LITERAL,
                f"--dest={self._vebus_service}",
                "/Devices/0/Ac/Inverter/P",
                GET_VALUE_METHOD,
            ],
            service=self._vebus_service,
            timeout=0.3,
        )
        if output:
            try:
                parts = output.strip().split()
                if parts:
                    self._system_data["inv_power"] = int(float(parts[-1]))
                    self._consecutive_errors = 0
            except (ValueError, TypeError) as e:
                logger.debug("Inverter power parse failed: %s", e)
                self._consecutive_errors += 1
        else:
            self._consecutive_errors += 1

    def _poll_daily_yields(self):
        """Poll daily/yesterday yields for MPPT chargers and Tasmota inverters (throttled to 5s)"""
        now = time.time()
        if now - self._last_daily_yields_time < 5.0:
            return

        # MPPT: /History/Daily/0 is today, /1 is yesterday (no lock - background thread)
        self._cached_mppt_daily_yields = [
            self._get_float_nolock(service, "/History/Daily/0/Yield")
            for service in self._mppt_services
        ]
        self._cached_mppt_yesterday_yields = [
            self._get_float_nolock(service, "/History/Daily/1/Yield")
            for service in self._mppt_services
        ]

        # Tasmota: dbus-tasmota-pv publishes both counters directly from the
        # plug telemetry (ENERGY.Today / ENERGY.Yesterday) - no arithmetic here.
        self._cached_tasmota_daily_yields = [
            self._get_float_nolock(s, TASMOTA_ENERGY_DAILY_PATH) for s in self._pv_inverter_services
        ]
        self._cached_tasmota_yesterday_yields = [
            self._get_float_nolock(s, TASMOTA_ENERGY_YESTERDAY_PATH)
            for s in self._pv_inverter_services
        ]
        self._last_daily_yields_time = now

    def _local_today(self) -> int:
        """Day-of-year in the user's timezone (/Settings/System/TimeZone).
        Falls back to system localtime (UTC on Venus) if setting is missing."""
        if not self._tz_name:
            self._tz_name = self._dbus_get(SETTINGS_SERVICE, "/Settings/System/TimeZone") or ""
        if self._tz_name:
            try:
                return datetime.now(ZoneInfo(self._tz_name)).timetuple().tm_yday
            except Exception as e:
                logger.warning("Timezone %s unavailable (%s), using system local", self._tz_name, e)
                self._tz_name = ""
        return time.localtime().tm_yday

    def _poll_battery_daily_energy(self):
        """Integrate battery power over time into daily charge/discharge kWh (5s tick).
        com.victronenergy.system has no /History/Daily paths on dbus-systemcalc-py
        systems, so we accumulate bp ourselves and reset at midnight."""
        now = time.time()
        if now - self._last_battery_daily_energy_time < 5.0:
            return

        today = self._local_today()
        if today != self._battery_energy_date:
            self._cached_battery_daily_energy = (0.0, 0.0)
            self._battery_energy_date = today
            self._persist_battery_daily_energy(now)

        bp = self._system_data.get("bp") or 0.0
        dt = now - self._battery_energy_last_time if self._battery_energy_last_time else 0.0
        if 0 < dt < 30:  # skip first sample and long gaps (restart/suspend)
            charge, discharge = self._cached_battery_daily_energy
            kwh = abs(bp) * dt / 3600000.0  # W*s -> kWh
            if bp > 0:
                charge += kwh
            else:
                discharge += kwh
            self._cached_battery_daily_energy = (round(charge, 4), round(discharge, 4))

        self._battery_energy_last_time = now
        if now - self._battery_energy_last_persist >= BATTERY_ENERGY_PERSIST_INTERVAL:
            self._persist_battery_daily_energy(now)
        self._last_battery_daily_energy_time = now

    def _persist_battery_daily_energy(self, now: float | None = None):
        """Save battery daily energy accumulators so a restart keeps today's totals."""
        self._battery_energy_last_persist = now if now is not None else time.time()
        try:
            with open(self._battery_energy_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "date": self._battery_energy_date,
                        "charge": self._cached_battery_daily_energy[0],
                        "discharge": self._cached_battery_daily_energy[1],
                    },
                    f,
                )
        except OSError as e:
            logger.debug("Battery energy persist failed: %s", e)

    def _load_battery_daily_energy(self):
        """Load battery daily energy accumulators from a previous run (same day only)."""
        try:
            with open(self._battery_energy_file, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == self._local_today():
                self._cached_battery_daily_energy = (
                    float(data.get("charge", 0.0)),
                    float(data.get("discharge", 0.0)),
                )
                self._battery_energy_date = int(data["date"])
        except (OSError, ValueError, TypeError):
            pass  # Missing or corrupt file -> start from zero

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

    def _service_healthy(self, service: str) -> bool:
        """Check if a D-Bus service is healthy (not in backoff period)."""
        backoff_until = self._service_backoff_until.get(service, 0.0)
        return time.time() >= backoff_until

    def _record_service_success(self, service: str) -> None:
        """Record a successful D-Bus response for a service."""
        self._service_consecutive_fails[service] = 0
        self._service_backoff_until[service] = 0.0

    def _record_service_failure(self, service: str) -> None:
        """Record a timeout/failure for a service, applying exponential backoff."""
        fails = self._service_consecutive_fails.get(service, 0) + 1
        self._service_consecutive_fails[service] = fails
        if fails >= self.SERVICE_FAIL_THRESHOLD:
            backoff = min(
                self.SERVICE_BACKOFF_BASE * (2 ** (fails - self.SERVICE_FAIL_THRESHOLD)),
                self.SERVICE_BACKOFF_MAX,
            )
            self._service_backoff_until[service] = time.time() + backoff
            logger.warning(
                "D-Bus service %s unresponsive (%d failures), backing off %.0fs",
                service,
                fails,
                backoff,
            )

    def _safe_subprocess_tracked(self, cmd: list, service: str, timeout: float = 0.3) -> str | None:
        """Run subprocess with service health tracking (for background poll).
        Returns None immediately if service is in backoff period."""
        if not self._service_healthy(service):
            return None
        result = self._safe_subprocess(cmd, timeout=timeout)
        if result is not None:
            self._record_service_success(service)
        else:
            self._record_service_failure(service)
        return result

    def _dbus_get(self, service: str, path: str) -> str | None:
        """Get a single value from D-Bus.
        Skips known-unresponsive services to avoid blocking the caller."""
        if not self._service_healthy(service):
            return None

        with self._dbus_lock:
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
                    self._record_service_success(service)
                    return parts[-1]

            # Track error
            self._consecutive_errors += 1
            self._record_service_failure(service)
            return None

    def _dbus_set(self, service: str, path: str, value: int, value_type: str = "int16") -> bool:
        """Set a value on D-Bus"""

        with self._dbus_lock:
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

        parsed = parse_system_data_output(output)
        data.update(parsed)
        return data

    def get_inverter_state(self) -> tuple[int, str]:
        """Get inverter state code and description - instant from background cache"""
        now = time.time()
        if now - self._last_inverter_state_time < 2.0:  # TTL 2 seconds
            return self._cached_inverter_state

        if not self._vebus_service:
            return 0, "Unknown"

        val = self._dbus_get(self._vebus_service, "/State")
        if val:
            try:
                result = self._parse_inverter_state_code(val)
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
        """
        if sys_data is None:
            sys_data = self.get_system_data()
        voltage = sys_data.get("bv", 0.0)
        power = sys_data.get("bp", 0)

        return calculate_battery_soc_from_voltage(voltage, power)

    def get_inverter_power(self) -> int:
        """Get current inverter AC output power - instant from background cache"""
        return self._system_data.get("inv_power", 0)

    def get_ac_in_power(self) -> int:
        """Get AC input power (from grid) - from system data cache"""
        return self._system_data.get("gt", 0)

    def set_grid_setpoint(self, watts: int) -> bool:
        """Set the grid power setpoint (Hub4/L1/AcPowerSetpoint)"""
        if not self._vebus_service:
            return False

        return self._dbus_set(self._vebus_service, "/Hub4/L1/AcPowerSetpoint", watts, "int16")

    def _query_mppt_sync(self, service: str) -> dict[str, float]:
        """Synchronously query a single MPPT for power and current"""
        power_output = self._safe_subprocess(
            [
                "dbus-send",
                "--system",
                PRINT_REPLY_LITERAL,
                f"--dest={service}",
                "/Yield/Power",
                GET_VALUE_METHOD,
            ],
            timeout=0.5,
        )
        current_output = self._safe_subprocess(
            [
                "dbus-send",
                "--system",
                PRINT_REPLY_LITERAL,
                f"--dest={service}",
                "/Dc/0/Current",
                GET_VALUE_METHOD,
            ],
            timeout=0.5,
        )
        return {
            "w": parse_variant_value(power_output),
            "a": parse_variant_value(current_output),
        }

    def get_mppt_data(self) -> dict[str, dict[str, float]]:
        """Get power and current from all MPPT chargers - instant from background cache"""
        now = time.time()
        if now - self._last_mppt_time < 0.5:  # TTL 0.5 seconds
            return self._cached_mppt_data

        if not self._mppt_services:
            return {}

        data = {
            f"mppt{i}": self._query_mppt_sync(service)
            for i, service in enumerate(self._mppt_services)
        }

        self._cached_mppt_data = data
        self._last_mppt_time = time.time()
        return data

    def get_pv_power(self) -> list:
        """Get power from PV inverters (Tasmota, ESPHome, etc.) via D-Bus - instant from background cache"""
        now = time.time()
        if now - self._last_pv_time < 0.5:  # TTL 0.5 seconds
            return self._cached_pv_powers

        powers = self._query_pv_powers(path=AC_POWER_PATH, reply_mode=PRINT_REPLY_LITERAL)
        self._cached_pv_powers = powers
        self._last_pv_time = time.time()
        return powers

    def get_acload_powers(self) -> dict[str, float]:
        """Get power from Emporia Vue channels (acload services) - instant from background cache"""
        now = time.time()
        if now - self._last_acload_time < 0.5:  # TTL 0.5 seconds
            return self._cached_acload_powers

        powers = self._query_acload_powers()
        self._cached_acload_powers = powers
        self._last_acload_time = time.time()
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
        """Get SoC for each battery chain from D-Bus - instant from background cache"""
        now = time.time()
        if now - self._last_battery_chain_soc_time < 2.0:  # TTL 2 seconds
            return self._cached_battery_chain_socs

        socs = self._query_battery_chain_socs(
            path="/Soc",
            reply_mode=PRINT_REPLY_LITERAL,
            soc_regex=VARIANT_RE,
        )
        self._cached_battery_chain_socs = socs
        self._last_battery_chain_soc_time = time.time()
        return socs

    def get_cell_counts(self) -> dict[str, int]:
        """Get discovered cell counts per battery chain service."""
        return dict(self._chain_cell_counts)

    def get_ess_mode(self) -> dict[str, Any]:
        """Get current ESS mode"""
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
        """Set ESS mode"""
        if external:
            result = self._dbus_set(SETTINGS_SERVICE, HUB4_MODE_PATH, 3, "int32")
        else:
            success1 = self._dbus_set(SETTINGS_SERVICE, HUB4_MODE_PATH, 1, "int32")
            success2 = self._dbus_set(
                SETTINGS_SERVICE,
                "/Settings/CGwacs/BatteryLife/State",
                0,
                "int32",
            )
            result = success1 and success2
        with self._dbus_lock:
            self._ess_mode_cache = None
            self._ess_mode_cache_time = 0.0
        return result

    @staticmethod
    def _parse_float_or_zero(raw: str) -> float:
        """Parse a float from raw output, returning 0.0 on failure or non-finite."""
        try:
            f = float(raw)
            return f if math.isfinite(f) else 0.0
        except (ValueError, TypeError):
            return 0.0

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

    def _get_float_nolock(self, service: str, path: str) -> float:
        """Read a D-Bus path using _safe_subprocess_tracked (no lock, for background thread)."""
        output = self._safe_subprocess_tracked(
            [
                "dbus-send",
                "--system",
                PRINT_REPLY_LITERAL,
                f"--dest={service}",
                path,
                GET_VALUE_METHOD,
            ],
            service=service,
            timeout=0.3,
        )
        # dbus-send --print-reply=literal prints "variant double <val>"; take last token
        parts = output.split()
        return self._parse_float_or_zero(parts[-1]) if parts else 0.0

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
        """Get detailed data for all battery chains including SmartShunt.
        Cached for 2s, queries services in parallel to avoid 4+ second freezes."""
        now = time.time()
        if self._cached_all_batteries and now - self._last_all_batteries_time < 2.0:
            return self._cached_all_batteries

        battery_services = [
            (BATTERY_CHAIN_1, "JBD Chain 1"),
            (BATTERY_CHAIN_2, "JBD Chain 2"),
            ("com.victronenergy.battery.virtual_chain", "Virtual Battery"),
        ]

        def _query_battery(service: str, name: str) -> dict[str, Any]:
            if not self._service_healthy(service):
                return {
                    "name": name,
                    "voltage": 0.0,
                    "current": 0.0,
                    "power": 0,
                    "soc": 0.0,
                    "state": "Unknown",
                    "time_to_go": "",
                    "time_to_go_sec": None,
                }
            current = self._get_float(service, DC_CURRENT_PATH)
            state = self._battery_state(current)
            ttg_sec = None
            ttg_raw = self._dbus_get(service, "/TimeToGo")
            if ttg_raw is not None:
                try:
                    ttg_sec = max(0, int(float(ttg_raw)))
                except (TypeError, ValueError):
                    pass
            return {
                "name": name,
                "voltage": self._get_float(service, "/Dc/0/Voltage"),
                "current": current,
                "power": self._get_float(service, "/Dc/0/Power"),
                "soc": self._get_float(service, "/Soc"),
                "state": state,
                "time_to_go": self._format_time_to_go(ttg_sec or 0, state),
                "time_to_go_sec": ttg_sec,
            }

        with ThreadPoolExecutor(max_workers=len(battery_services)) as pool:
            futures = [pool.submit(_query_battery, svc, name) for svc, name in battery_services]
            batteries = [f.result() for f in futures]

        self._cached_all_batteries = batteries
        self._last_all_batteries_time = now
        return batteries

    def _read_chain_cell_voltages(self, service: str, offset: int) -> list[tuple[float, int]]:
        """Probe cell voltage paths for a chain, updating the cached cell count."""
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
                pass

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
                if -50 <= t <= 100:
                    temps.append(t)
            except (ValueError, TypeError):
                pass
        return temps

    def _read_chain_soc(self, service: str) -> float | None:
        soc_val = self._dbus_get(service, "/Soc")
        if soc_val is None:
            return None
        try:
            return float(soc_val)
        except (ValueError, TypeError):
            return None

    def _read_chain_allow_flag(self, service: str, path: str) -> bool | None:
        val = self._dbus_get(service, path)
        if val is None:
            return None
        try:
            return int(float(val)) == 1
        except (ValueError, TypeError):
            return None

    def get_battery_cell_data(self) -> dict[str, Any]:
        """Get detailed cell data from battery chains for DVCC calculation."""
        self._maybe_refresh_cell_cache()
        cached = self._build_from_cache()
        if cached:
            return cached
        if getattr(self, "_poll_thread", None) and self._poll_thread.is_alive():
            return {}
        return self._get_battery_cell_data_live()

    def _maybe_refresh_cell_cache(self) -> None:
        """Refresh cache if stale. Non-blocking: skips if background thread will handle it."""
        if time.time() - self._last_battery_cell_data_time >= CELL_DATA_POLL_INTERVAL:
            if getattr(self, "_poll_thread", None) and self._poll_thread.is_alive():
                return
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
        """Get detailed data for all MPPT chargers.
        Cached for 2s, queries services in parallel."""
        now = time.time()
        if self._cached_mppt_chargers and now - self._last_mppt_chargers_time < 2.0:
            return self._cached_mppt_chargers

        def _query_mppt_charger(i: int, service: str) -> dict[str, Any]:
            parts = service.split(":")
            name = f"MPPT-{parts[1]}" if len(parts) > 1 else f"MPPT-{i}"
            return {
                "name": name,
                "pv_voltage": self._get_float(service, "/Pv/V"),
                "current": self._get_float(service, DC_CURRENT_PATH),
                "power": self._get_float(service, "/Yield/Power"),
            }

        with ThreadPoolExecutor(max_workers=len(self._mppt_services)) as pool:
            futures = [
                pool.submit(_query_mppt_charger, i, svc)
                for i, svc in enumerate(self._mppt_services)
            ]
            chargers = [f.result() for f in futures]

        self._cached_mppt_chargers = chargers
        self._last_mppt_chargers_time = now
        return chargers

    def get_mppt_daily_yields(self) -> list[float]:
        """Get daily yield (kWh) for each MPPT charger - instant from background cache"""
        return list(self._cached_mppt_daily_yields)

    def get_pv_inverter_daily_yields(self) -> list[float]:
        """Get daily yield (kWh) for each Tasmota PV inverter - instant from background cache"""
        return list(self._cached_tasmota_daily_yields)

    def get_mppt_yesterday_yields(self) -> list[float]:
        """Get yesterday's yield (kWh) for each MPPT charger - instant from background cache"""
        return list(self._cached_mppt_yesterday_yields)

    def get_pv_inverter_yesterday_yields(self) -> list[float]:
        """Get yesterday's yield (kWh) for each Tasmota PV inverter - instant from cache"""
        return list(self._cached_tasmota_yesterday_yields)

    def get_battery_daily_energy(self) -> tuple[float, float]:
        """Get battery daily charge/discharge energy (kWh) - instant from background cache"""
        return self._cached_battery_daily_energy

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

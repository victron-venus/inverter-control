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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import INVERTER_STATES, USE_NATIVE_DBUS
from .dbus_native import NativeDbusClient
from .victron_parse import (
    calculate_battery_soc_from_voltage,
    parse_shunt_data_output,
    parse_system_data_output,
)

logger = logging.getLogger("inverter-control")

# D-Bus path constants
DC_CURRENT_PATH = "/Dc/0/Current"
SETTINGS_SERVICE = "com.victronenergy.settings"
HUB4_MODE_PATH = "/Settings/CGwacs/Hub4Mode"
TOU_START_SETTING = "/Settings/InverterControl/TouExpensiveStartHour"
TOU_END_SETTING = "/Settings/InverterControl/TouExpensiveEndHour"
SYSTEM_SERVICE = "com.victronenergy.system"
GET_VALUE_METHOD = "com.victronenergy.BusItem.GetValue"
PRINT_REPLY_LITERAL = "--print-reply=literal"
TASMOTA_ENERGY_FORWARD_PATH = "/Ac/Energy/Forward"
TASMOTA_ENERGY_DAILY_PATH = "/Ac/Energy/Daily"
# Published by dbus-tasmota-pv >= 3.0 (Tasmota ENERGY.Yesterday)
TASMOTA_ENERGY_YESTERDAY_PATH = "/Energy/Daily/Yesterday"
AC_POWER_PATH = "/Ac/Power"
YIELD_POWER_PATH = "/Yield/Power"
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

# Fast inputs driven by BusItem PropertiesChanged signals instead of the 5Hz
# tree polls; the tree queries remain only as a slow reconciliation pass
# against missed events. gt/tt are derived (g1+g2 / t1+t2) like the parser.
SYSTEM_SIGNAL_PATHS = {
    "/Ac/Grid/L1/Power": "g1",
    "/Ac/Grid/L2/Power": "g2",
    "/Ac/Consumption/L1/Power": "t1",
    "/Ac/Consumption/L2/Power": "t2",
    "/Dc/Pv/Power": "pv_total",
}
# Bank V/I/P come straight from the SmartShunt — the ground-truth meter for
# the whole bank. The system/0 Dc/Battery aggregate only equals the shunt
# while virtual_chain is healthy (it is derived: shunt - chain1 - chain2),
# so reading the aggregate adds a failure mode without adding information.
# No fallback: if the shunt is missing the values stay at 0 until it is found.
SHUNT_SIGNAL_PATHS = {
    "/Dc/0/Voltage": "bv",
    "/Dc/0/Current": "bc",
    "/Dc/0/Power": "bp",
}
# Core keys hot-path consumers (calculate_setpoint, console format_line) index
# directly with []. The signal path updates _system_data one key at a time, so
# the cached dict can hold only a subset briefly at startup; get_system_data
# merges over these defaults so a partial cache never KeyErrors.
_SYSTEM_DATA_KEYS = frozenset({"g1", "g2", "gt", "t1", "t2", "tt", "bv", "bc", "bp"})
# MPPT chargers, Tasmota PV inverters and Vue acloads are signal-driven too;
# their single-value reads remain only as a slow reconciliation pass.
MPPT_SIGNAL_PATHS = {
    YIELD_POWER_PATH: "w",
    DC_CURRENT_PATH: "a",
}
PV_SIGNAL_PATHS = {"/Ac/Power": "p"}
ACLOAD_NAME_PATH = "/CustomName"
VEBUS_STATE_PATH = "/State"
VEBUS_INV_POWER_PATH = "/Devices/0/Ac/Inverter/P"
# While signals are healthy, hot tree polls run only this often
SIGNAL_RECONCILE_INTERVAL = 30.0
# Getter cache TTLs must exceed the worst-case background refresh cadence
# (UNHEALTHY_POLL_INTERVAL), or control-loop getters fall into sync reads.
GROUP_CACHE_TTL = 1.2
# When the signal path is down, tree polls back off from every 5Hz pass to
# this cadence (matches the control-thread staleness budget of ~1s).
UNHEALTHY_POLL_INTERVAL = 1.0
# Failed subscribe attempts are retried at this cadence (vebus may simply not
# be up yet during boot; a mid-run daemon drop needs faster repair than 60s).
SIGNAL_SETUP_RETRY_INTERVAL = 10.0
# A healthy-flagged path that produces no data for this long is treated as
# dead: subscriptions can silently vanish (daemon restart drops match rules),
# so the flag must be re-derived from observed traffic, not trusted forever.
SIGNAL_SILENCE_TIMEOUT = 10.0


class VictronDBus:
    """
    Fast D-Bus interface for Victron system.

    Get/Set go through a persistent dbus_fast connection (see dbus_native)
    with dbus-send subprocess fallback; background telemetry rides the same
    connection via change signals plus a slow native-first reconcile pass.
    """

    # Total dbus-send invocations (perf metric; class default covers
    # instances created without __init__, e.g. in tests)
    subprocess_calls = 0

    # Auto-rescan thresholds
    RESCAN_ERROR_THRESHOLD = 5  # Rescan after N consecutive errors
    RESCAN_INTERVAL_SECONDS = (
        1800  # Rescan every 30 minutes regardless (fallback for event-driven discovery)
    )
    RESCAN_COOLDOWN_SECONDS = 60  # Minimum time between error-triggered rescans
    # Timeout for the `dbus -y` service-listing subprocess. Generous: on a
    # heavily loaded GX the D-Bus daemon answers slowly (2 s timed out during
    # the 2026-08-27 load 5-7 incident), which was logged as a false
    # "discovery failed" every minute. Discovery runs off the control-loop hot
    # path, so a few seconds here does not delay setpoints.
    DISCOVERY_TIMEOUT = 5.0

    # Service health tracking: after N consecutive timeouts, back off
    SERVICE_FAIL_THRESHOLD = 3  # Consecutive failures before backing off
    SERVICE_BACKOFF_BASE = 10.0  # Initial backoff: 10s
    SERVICE_BACKOFF_MAX = 300.0  # Max backoff: 5 minutes
    SERVICE_PROBE_INTERVAL = 30.0  # How often to probe backed-off services

    def __init__(self, test_mode: bool = False):
        self._vebus_service: str | None = None
        self._shunt_service: str | None = None
        self._mppt_services: list = []
        self._consecutive_errors: int = 0
        self._last_scan_time: float = 0
        self._last_success_time: float = 0
        self._last_rescan_time: float = 0  # Cooldown tracker for error-triggered rescans
        self._dbus_lock = threading.Lock()
        # Setpoint writes get their own lock so a telemetry read holding
        # _dbus_lock can never delay the control-loop write path.
        self._set_lock = threading.Lock()
        # Serializes background service discovery (startup, NameOwnerChanged on
        # the native signal thread, and the poll-thread rescan can otherwise
        # run overlapping `dbus -y` subprocesses and race the service maps).
        self._discovery_lock = threading.Lock()
        # Persistent native D-Bus connection (None in test mode / disabled)
        self._native: NativeDbusClient | None = None
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
        # Last known-good SoC per chain service, retained so a transient read
        # failure (e.g. mqtt_chain1 unresponsive, 2026-08-27) never collapses a
        # live chain to a fake 0.0% that DVCC/control would act on.
        self._last_known_chain_soc: dict[str, float] = {}
        # Cache for inverter state
        self._cached_inverter_state: tuple[int, str] = (0, "Unknown")
        self._last_inverter_state_time: float = 0.0
        # Cache for acload (Emporia Vue) power channels: per-service reads
        # composed into the named {CustomName: power} view on demand
        self._acload_services: list = []
        self._acload_names: dict[str, str] = {}
        self._acload_powers_by_service: dict[str, float] = {}
        self._last_acload_time: float = 0.0
        # Cache for discovered Tasmota PV inverter services
        self._pv_inverter_services: list = []
        # Cache for daily/yesterday yields (MPPT + Tasmota) and battery daily energy
        self._cached_mppt_daily_yields: list[float] = []
        self._cached_pv_inverter_daily_yields: list[float] = []
        self._cached_mppt_yesterday_yields: list[float] = []
        self._cached_pv_inverter_yesterday_yields: list[float] = []
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
        # Yesterday's totals, promoted from today's at the local-midnight rollover
        self._battery_energy_yesterday: tuple[float, float] = (0.0, 0.0)
        # Service health tracking: detect unresponsive D-Bus services
        # and back off to avoid 4+ second freezes from sequential timeouts.
        self._service_consecutive_fails: dict[str, int] = {}
        self._service_backoff_until: dict[str, float] = {}
        # Venus OS system clock runs UTC; user timezone lives in localsettings,
        # read lazily on first _local_today() (localsettings may lag at boot).
        self._tz_name: str = ""
        self._load_battery_daily_energy()

        self._test_mode = test_mode
        # D-Bus discovery failure log throttling (once per minute)
        self._last_discovery_failed_log: float = 0.0

        if not test_mode and USE_NATIVE_DBUS:
            self._native = NativeDbusClient()
            # Set up NameOwnerChanged handler for service discovery
            self._native.add_name_owner_handler(self._on_name_owner_changed)

        # Signal-driven fast inputs (see SYSTEM_SIGNAL_PATHS)
        self._signal_paths_subscribed = False
        self._signal_handler_attached = False
        self._last_signal_reconcile = 0.0
        self._last_signal_setup_try = 0.0
        self._last_signal_ok_monotonic: float | None = None
        self._next_unhealthy_poll = 0.0

        # Background polling thread (like HA does) - skip in test mode
        self._poll_thread: threading.Thread | None = None
        self._poll_stop_event = threading.Event()
        self._poll_interval = 0.2  # Poll at 5Hz, faster than control loop (3Hz)
        self._system_data: dict[str, Any] = {}  # Populated by background polling
        if not test_mode:
            self._start_background_polling()

        self._discover_services()

    def _fast_targets(self) -> list[tuple[str, str]]:
        """(service, path) pairs for all signal-driven fast inputs."""
        targets = [(SYSTEM_SERVICE, path) for path in SYSTEM_SIGNAL_PATHS]
        if self._shunt_service:
            targets.extend((self._shunt_service, path) for path in SHUNT_SIGNAL_PATHS)
        if self._vebus_service:
            targets.append((self._vebus_service, VEBUS_STATE_PATH))
            targets.append((self._vebus_service, VEBUS_INV_POWER_PATH))
        for service in self._mppt_services:
            targets.extend((service, path) for path in MPPT_SIGNAL_PATHS)
        for service in self._pv_inverter_services:
            targets.extend((service, path) for path in PV_SIGNAL_PATHS)
        for service in self._acload_services:
            targets.append((service, ACLOAD_NAME_PATH))
            targets.append((service, AC_POWER_PATH))
        return targets

    def _setup_fast_signals(self):
        """Subscribe to BusItem change signals for the fast inputs and seed values.

        com.victronenergy.system (dbus-systemcalc-py) announces changes via the
        bulk ItemsChanged snapshot on "/" and not per-item signals, so both
        shapes are armed. Signals fire on change only, so current values are
        fetched once here; the reconnect hook re-seeds them after a drop."""
        if self._native is None:
            return
        self._last_signal_setup_try = time.time()
        if not self._signal_handler_attached:
            self._native.on_reconnect = self._seed_fast_values
            self._native.add_signal_handler(self._on_fast_signal)
            self._signal_handler_attached = True

        subscribed = [self._native.subscribe_service_items(SYSTEM_SERVICE)]
        if self._shunt_service:
            subscribed.append(self._native.subscribe_service_items(self._shunt_service))
        if self._vebus_service:
            # vebus is a C++ service; arm both signal shapes for it
            subscribed.append(self._native.subscribe_service_items(self._vebus_service))
            subscribed.append(self._native.subscribe_busitem(self._vebus_service, VEBUS_STATE_PATH))
            subscribed.append(
                self._native.subscribe_busitem(self._vebus_service, VEBUS_INV_POWER_PATH)
            )
        # Discovered services (solarcharger C++, tasmota-pv/acload velib-python)
        for service in (*self._mppt_services, *self._pv_inverter_services, *self._acload_services):
            subscribed.append(self._native.subscribe_service_items(service))
        self._set_signals_healthy(all(subscribed))
        if all(subscribed):
            self._last_signal_ok_monotonic = time.monotonic()
        else:
            logger.debug(
                "Fast signal subscribe incomplete (%d/%d)", sum(subscribed), len(subscribed)
            )

        self._seed_fast_values()

    def _seed_fast_values(self):
        """Fetch current values for all subscribed paths (initial/reconnect)."""
        if self._native is None:
            return
        for service, path in self._fast_targets():
            self._apply_fast_value(service, path, self._native.get_value(service, path))
        self._last_signal_reconcile = time.time()

    def _signals_healthy(self) -> bool:
        """True when all fast-input subscriptions are armed on the live bus."""
        return bool(self._native is not None and self._signal_paths_subscribed)

    def is_signals_healthy(self) -> bool:
        """Public view of fast-signal path health (for perf telemetry)."""
        return self._signals_healthy()

    def _set_signals_healthy(self, value: bool) -> None:
        """Update subscription flag; log each healthy<->unhealthy flip once."""
        if value == self._signal_paths_subscribed:
            return
        self._signal_paths_subscribed = value
        if value:
            logger.info("Fast signal path healthy: subscriptions armed")
        else:
            logger.warning("Fast signal path unhealthy: tree polling fallback engaged")

    def _on_fast_signal(self, service: str | None, path: str, raw: str | None):
        """BusItem change handler: routes one payload into the caches."""
        self._apply_fast_value(service, path, raw)

    def _apply_fast_value(self, service: str | None, path: str, raw: str | None):
        """Apply one fast-input reading (signal or seed fetch) to the caches.

        Routing is by (service, path), never path alone: vebus's bulk
        ItemsChanged snapshot carries its own /Dc/0/{Power,Current,Voltage}
        items whose values are the Multi's coarse DC accounting, not the
        SmartShunt's bank truth — a path-only route let them overwrite bp/bc/bv
        every snapshot and made battery power flicker between two realities.
        """
        if raw is None:
            return
        if service == SYSTEM_SERVICE:
            key = SYSTEM_SIGNAL_PATHS.get(path)
        elif service is not None and service == self._shunt_service:
            key = SHUNT_SIGNAL_PATHS.get(path)
        elif service is not None and service == self._vebus_service:
            if path == VEBUS_STATE_PATH:
                code = int(float(raw))
                self._cached_inverter_state = (code, INVERTER_STATES.get(code, f"? ({code})"))
                self._last_inverter_state_time = time.time()
            elif path == VEBUS_INV_POWER_PATH:
                self._system_data["inv_power"] = int(float(raw))
            # Observed traffic proves the signal path alive; see _poll_all.
            self._last_signal_ok_monotonic = time.monotonic()
            return
        elif service is not None and self._apply_group_value(service, path, raw):
            # Polled-group value (MPPT/PV/acload) routed; observed traffic
            # proves the signal path alive; see _poll_all.
            self._last_signal_ok_monotonic = time.monotonic()
            return
        else:
            return  # unknown sender: not a subscribed fast input, ignore
        if key is None:
            return
        # Observed traffic proves the signal path alive; see _poll_all.
        self._last_signal_ok_monotonic = time.monotonic()
        now = time.time()
        try:
            val = float(raw)
            self._system_data[key] = round(val) if key not in ("bv", "bc") else val
            self._system_data["gt"] = int(
                self._system_data.get("g1", 0) + self._system_data.get("g2", 0)
            )
            self._system_data["tt"] = int(
                self._system_data.get("t1", 0) + self._system_data.get("t2", 0)
            )
            self._system_data["_last_update"] = now
        except (ValueError, TypeError):
            pass  # non-numeric payload (e.g. NULL variant); next event fixes it

    def _apply_group_value(self, service: str | None, path: str, raw: str) -> bool:
        """Route one polled-group reading (MPPT/PV/acload) into its cache.

        Returns False for senders outside these groups so the caller ignores
        them; True means routed — even for unparseable payloads, which still
        prove the sender's match rules are alive."""
        if service is None or (
            service not in self._mppt_services
            and service not in self._pv_inverter_services
            and service not in self._acload_services
        ):
            return False
        if service in self._acload_services and path == ACLOAD_NAME_PATH:
            self._acload_names[service] = raw.strip()
            return True  # string payload; handled before the numeric parse below
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return True  # NULL variant etc.; next event carries a real value
        now = time.time()
        if service in self._mppt_services and path in MPPT_SIGNAL_PATHS:
            slot = self._cached_mppt_data.setdefault(
                f"mppt{self._mppt_services.index(service)}", {"w": 0.0, "a": 0.0}
            )
            slot[MPPT_SIGNAL_PATHS[path]] = val
            self._last_mppt_time = now
        elif service in self._pv_inverter_services and path in PV_SIGNAL_PATHS:
            idx = self._pv_inverter_services.index(service)
            powers = list(self._cached_pv_powers)
            while len(powers) <= idx:
                powers.append(0.0)
            powers[idx] = val
            self._cached_pv_powers = powers[: len(self._pv_inverter_services)]
            self._last_pv_time = now
        elif service in self._acload_services and path == AC_POWER_PATH:
            self._acload_powers_by_service[service] = val
            self._last_acload_time = now
        else:
            return False  # subscribed sender, but a path we don't track
        return True

    def _compose_acload_powers(self) -> dict[str, float]:
        """Named {CustomName: power} view of the per-service acload caches."""
        return {
            self._acload_names[svc]: self._acload_powers_by_service[svc]
            for svc in self._acload_services
            if svc in self._acload_names and svc in self._acload_powers_by_service
        }

    def _discover_services(self):
        """Discover VE.Bus, MPPT, acload, and Tasmota PV inverter services.

        Runs a blocking `dbus -y` subprocess (bounded by DISCOVERY_TIMEOUT) that
        takes the discovery lock; concurrent callers (startup, NameOwnerChanged,
        poll-thread rescan) are skipped rather than queued so a slow daemon never
        piles up overlapping subprocesses. Must NOT be called while holding
        _dbus_lock."""

        if not self._discovery_lock.acquire(blocking=False):
            # Another discovery is already in flight; skip rather than queue so
            # a loaded daemon can't trigger a backlog of duplicate scans.
            return
        try:
            self._last_scan_time = time.time()
            old_vebus = self._vebus_service

            try:
                result = subprocess.run(
                    ["dbus", "-y"],
                    capture_output=True,
                    text=True,
                    timeout=self.DISCOVERY_TIMEOUT,
                    check=False,
                )
            except Exception as e:
                now = time.time()
                if now - self._last_discovery_failed_log >= 60.0:
                    logger.debug("D-Bus service discovery failed: %s", e)
                    self._last_discovery_failed_log = now
                return
            lines = result.stdout.strip().split("\n")

            self._vebus_service = None
            self._mppt_services = []
            self._acload_services = []
            self._pv_inverter_services = []
            battery_candidates: list[str] = []

            (
                self._vebus_service,
                self._mppt_services,
                self._acload_services,
                self._pv_inverter_services,
                battery_candidates,
            ) = self._process_discovered_lines(lines)
            self._mppt_services.sort()
            self._acload_services.sort()
            self._pv_inverter_services.sort()

            # The SmartShunt's bus-name suffix (ttyUSB4 today) can change
            # across GX reboots, so match by ProductName, never by instance.
            old_shunt = self._shunt_service
            self._shunt_service = None
            for candidate in battery_candidates:
                if "shunt" in self._read_product_name(candidate).lower():
                    self._shunt_service = candidate
                    break

            # Log if service changed
            if old_vebus and self._vebus_service and old_vebus != self._vebus_service:
                print(f"  [D-Bus] VE.Bus service changed: {old_vebus} -> {self._vebus_service}")
            elif not old_vebus and self._vebus_service:
                print(f"  [D-Bus] VE.Bus service found: {self._vebus_service}")

            if old_shunt != self._shunt_service:
                if self._shunt_service:
                    print(f"  [D-Bus] SmartShunt service found: {self._shunt_service}")
                else:
                    print("  [D-Bus] SmartShunt service not found")

            if self._acload_services:
                print(f"  [D-Bus] acload services found: {len(self._acload_services)}")

            if self._pv_inverter_services:
                print(f"  [D-Bus] PV inverters found: {self._pv_inverter_services}")

            self._consecutive_errors = 0

            if self._native is not None:
                # Service set changed: re-arm subscriptions and force an
                # immediate reconcile so fresh services seed their caches.
                self._last_signal_reconcile = 0.0
                self._setup_fast_signals()

        except Exception as e:
            now = time.time()
            if now - self._last_discovery_failed_log >= 60.0:
                logger.debug("D-Bus service discovery failed: %s", e)
                self._last_discovery_failed_log = now
        finally:
            self._discovery_lock.release()

    def _process_discovered_lines(self, lines):
        """Process the lines from dbus -y to discover services."""
        vebus_service = None
        mppt_services = []
        acload_services = []
        pv_inverter_services = []
        battery_candidates = []
        for line in lines:
            if "com.victronenergy.vebus" in line:
                vebus_service = line.strip()
            elif "com.victronenergy.solarcharger" in line:
                mppt_services.append(line.strip())
            elif "com.victronenergy.acload" in line:
                acload_services.append(line.strip())
            elif "com.victronenergy.pvinverter." in line:
                pv_inverter_services.append(line.strip())
            elif "com.victronenergy.battery." in line:
                battery_candidates.append(line.strip())
        return (
            vebus_service,
            mppt_services,
            acload_services,
            pv_inverter_services,
            battery_candidates,
        )

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

    def _on_name_owner_changed(self, service_name: str, old_owner: str, new_owner: str):
        """Handle NameOwnerChanged signals to trigger service discovery when services appear/disappear.

        This replaces the periodic RESCAN_INTERVAL_SECONDS timer with event-driven discovery.
        We trigger discovery when:
        - A service gains an owner (appears on the bus)
        - A service loses its owner (disappears from the bus)
        """
        # Only trigger discovery for services we care about
        tracked_services = {
            SYSTEM_SERVICE,
            SETTINGS_SERVICE,
            BATTERY_CHAIN_1,
            BATTERY_CHAIN_2,
        }

        # Also check for any ve bus, mptt, ac load, or pv inverter services
        if service_name.startswith(
            (
                "com.victronenergy.vebus",
                "com.victronenergy.solarcharger",
                "com.victronenergy.acload",
                "com.victronenergy.pvinverter.",
                "com.victronenergy.battery.",
            )
        ):
            tracked_services.add(service_name)

        if service_name in tracked_services:
            logger.debug(f"NameOwnerChanged: {service_name} {old_owner} -> {new_owner}")
            self._discover_services()

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

        # A healthy-flagged path that has gone silent is lying: match rules
        # can vanish on a daemon restart without any error surfacing. Re-derive
        # the flag from observed traffic so repair (and the health metric)
        # engage during mid-run outages, not only after boot failures.
        if (
            self._signals_healthy()
            and self._last_signal_ok_monotonic is not None
            and time.monotonic() - self._last_signal_ok_monotonic > SIGNAL_SILENCE_TIMEOUT
        ):
            logger.warning("No fast-signal data for %.0fs - resubscribing", SIGNAL_SILENCE_TIMEOUT)
            self._set_signals_healthy(False)

        # Fast inputs (grid/battery/inverter) arrive via PropertiesChanged
        # signals while subscriptions are healthy; tree polls then only
        # reconcile every SIGNAL_RECONCILE_INTERVAL against missed events.
        if self._signals_healthy():
            if time.time() - self._last_signal_reconcile >= SIGNAL_RECONCILE_INTERVAL:
                self._poll_system_data()
                self._poll_shunt_data()
                self._poll_inverter_power()
                self._reconcile_mppt_data()
                self._reconcile_pv_power()
                self._reconcile_acload_power()
                self._last_signal_reconcile = time.time()
        else:
            if (
                self._native is not None
                and time.time() - self._last_signal_setup_try > SIGNAL_SETUP_RETRY_INTERVAL
            ):
                self._setup_fast_signals()  # retry failed subscriptions (e.g. boot)
            # Tree polls, throttled: a dbus-send spawn per 5Hz pass was the
            # storm behind the watchdog restart loop. Native-first reconciles
            # ride along so no cache goes stale while signals are down.
            now_mono = time.monotonic()
            if now_mono >= self._next_unhealthy_poll:
                self._next_unhealthy_poll = now_mono + UNHEALTHY_POLL_INTERVAL
                self._poll_system_data()
                self._poll_shunt_data()
                self._reconcile_mppt_data()
                self._reconcile_pv_power()
                self._reconcile_acload_power()

        # Poll battery chain SoCs (already fork-free native reads)
        self._poll_battery_chain_socs()

        # Poll inverter state (native-first; keeps the main-thread getter pure-cache)
        self._poll_inverter_state()

        # Gated group reconciles (MPPT/PV/acload) run every pass in the
        # BACKGROUND thread. Signals keep _last_*_time fresh while responsive;
        # the gate skips redundant work then. When signals are quiet, the cache
        # timestamp goes stale and this refresh happens HERE instead of the main
        # control thread falling into a synchronous reconcile in a getter.
        self._reconcile_groups_if_stale()

        # Poll battery chain cell data (throttled to every 30s)
        self._poll_battery_cell_data_tree()

        # Poll daily yields and battery energy (throttled to every 5s)
        self._poll_daily_yields()
        self._poll_battery_daily_energy()

    def _reconcile_groups_if_stale(self):
        """Refresh MPPT/PV/acload/ESS/battery caches in the poll thread when their
        getter cache TTL has lapsed. Keeps the control loop getters pure-cache:
        the main thread never performs a synchronous reconcile."""
        now = time.time()
        if self._mppt_services and now - self._last_mppt_time >= GROUP_CACHE_TTL:
            self._reconcile_mppt_data()
        if self._pv_inverter_services and now - self._last_pv_time >= GROUP_CACHE_TTL:
            self._reconcile_pv_power()
        if self._acload_services and now - self._last_acload_time >= GROUP_CACHE_TTL:
            self._reconcile_acload_power()
        if self._ess_mode_cache and now - self._ess_mode_cache_time >= 5.0:
            self._reconcile_ess_mode()
        if (self._cached_all_batteries or self._last_all_batteries_time == 0) and (
            now - self._last_all_batteries_time >= 2.0
        ):
            self._reconcile_all_batteries()

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

    def _poll_shunt_data(self):
        """Poll bank V/I/P from the SmartShunt service (tree query)."""
        if not self._shunt_service:
            return
        output = self._safe_subprocess_tracked(
            [
                "dbus-send",
                "--system",
                "--print-reply",
                f"--dest={self._shunt_service}",
                "/",
                GET_VALUE_METHOD,
            ],
            service=self._shunt_service,
            timeout=0.5,
        )
        if output:
            self._system_data.update(parse_shunt_data_output(output))
            self._system_data["_last_update"] = time.time()

    def _read_product_name(self, service: str) -> str:
        """Product name of a battery service ('' when unreadable)."""
        if self._native is not None:
            value = self._native.get_value(service, "/ProductName")
            if value:
                return str(value)
            logger.debug(
                "D-Bus native read failed, falling back to dbus-send: %s /ProductName", service
            )
        result = self._safe_subprocess(
            [
                "dbus-send",
                "--system",
                PRINT_REPLY_LITERAL,
                f"--dest={service}",
                "/ProductName",
                GET_VALUE_METHOD,
            ],
            timeout=0.5,
        )
        match = re.search(r'string\s+"([^"]*)"', result or "")
        return match.group(1) if match else ""

    def _reconcile_mppt_data(self):
        """Refresh MPPT power/current via native-first single-value reads.

        Signals carry live updates while subscriptions are healthy; this slow
        pass (30s reconcile / fallback polling) rebuilds the map against
        missed or dropped events."""
        data = {}
        for i, service in enumerate(self._mppt_services):
            data[f"mppt{i}"] = {
                "w": self._get_float_nolock(service, YIELD_POWER_PATH),
                "a": self._get_float_nolock(service, DC_CURRENT_PATH),
            }
        self._cached_mppt_data = data
        self._last_mppt_time = time.time()

    def _reconcile_pv_power(self):
        """Refresh PV inverter powers (native-first; see _reconcile_mppt_data)."""
        self._cached_pv_powers = [
            self._get_float_nolock(service, AC_POWER_PATH) for service in self._pv_inverter_services
        ]
        self._last_pv_time = time.time()

    def _reconcile_acload_power(self):
        """Refresh Emporia Vue channels (native-first; see _reconcile_mppt_data)."""
        for service in self._acload_services:
            name = self._dbus_get(service, ACLOAD_NAME_PATH)
            if name:
                self._acload_names[service] = name.strip()
        self._acload_powers_by_service = {
            service: self._get_float_nolock(service, AC_POWER_PATH)
            for service in self._acload_services
        }
        self._last_acload_time = time.time()

    def _query_battery_chain_socs(self) -> list[float]:
        """Query all battery chain services for SoC (shared by poll and fallback).

        Goes through the native-first _dbus_get helper on purpose: the
        mqtt-chain battery services never answer a plain dbus-send call, so
        the previous CLI-only read here failed every cycle, accumulated
        per-service failures and tripped the backoff that zeroed out
        get_all_batteries().
        """
        socs = []
        for service in (BATTERY_CHAIN_1, BATTERY_CHAIN_2):
            val = self._dbus_get(service, "/Soc")
            try:
                soc = float(val) if val is not None else None
            except (TypeError, ValueError):
                logger.debug("Battery chain SoC parse failed: %s", val)
                soc = None
            if soc is not None:
                self._last_known_chain_soc[service] = soc
                socs.append(soc)
            else:
                # Fall back to the last known-good value for this chain instead
                # of a fabricated 0.0 (a 0% SoC would wrongly throttle DVCC).
                socs.append(self._last_known_chain_soc.get(service, 0.0))
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
        # Extract integer from literal output, e.g., "variant       uint32 3"
        parts = raw.strip().split()
        if not parts:
            raise ValueError(f"Empty inverter state output: {raw!r}")
        try:
            code = int(parts[-1])
        except ValueError:
            raise ValueError(f"No integer found in inverter state output: {raw!r}")
        return code, INVERTER_STATES.get(code, f"? ({code})")

    def _poll_inverter_state(self):
        """Poll inverter state (native-first read; refreshes the cache each pass)

        Runs every 5Hz poll pass so the main-thread getter never has to do a
        synchronous D-Bus read (or subprocess) for /State."""
        if not self._vebus_service:
            self._cached_inverter_state = (0, "Unknown")
            self._last_inverter_state_time = time.time()
            return

        val = self._dbus_get_native_only(self._vebus_service, VEBUS_STATE_PATH)
        if val:
            try:
                result = self._parse_inverter_state_code(val)
                self._cached_inverter_state = result
                self._consecutive_errors = 0
            except (ValueError, TypeError) as e:
                logger.debug("Inverter state parse failed: %s", e)
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
            timeout=0.5,
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
        self._cached_pv_inverter_daily_yields = [
            self._get_float_nolock(s, TASMOTA_ENERGY_DAILY_PATH) for s in self._pv_inverter_services
        ]
        self._cached_pv_inverter_yesterday_yields = [
            self._get_float_nolock(s, TASMOTA_ENERGY_YESTERDAY_PATH)
            for s in self._pv_inverter_services
        ]
        self._last_daily_yields_time = now

    def _local_now(self) -> datetime:
        """Now in the user's timezone (/Settings/System/TimeZone).
        Falls back to system localtime (UTC on Venus) if setting is missing."""
        if not self._tz_name:
            self._tz_name = self._dbus_get(SETTINGS_SERVICE, "/Settings/System/TimeZone") or ""
        if self._tz_name:
            try:
                return datetime.now(ZoneInfo(self._tz_name))
            except Exception as e:
                logger.warning("Timezone %s unavailable (%s), using system local", self._tz_name, e)
                self._tz_name = ""
        return datetime.fromtimestamp(time.time()).astimezone()

    def get_local_hour(self) -> int:
        """Hour-of-day in the user's timezone (/Settings/System/TimeZone)."""
        return self._local_now().hour

    @staticmethod
    def _tou_setting_int(raw: str | None) -> int | None:
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def get_tou_setting(self, path: str) -> int | None:
        """Read an integer InverterControl setting (None if missing/unreadable)."""
        return self._tou_setting_int(self._dbus_get(SETTINGS_SERVICE, path))

    def _dbus_add_setting(self, group: str, name: str, value: int) -> bool:
        """Create a localsettings entry via com.victronenergy.Settings.AddSetting.

        SetValue on a nonexistent path is rejected - new settings must be
        registered through AddSetting (group, name, default variant:int32,
        type 'i', min/max variant:int32). Returns True on completion code 0.
        """
        with self._dbus_lock:
            result = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    "--type=method_call",
                    "--print-reply",
                    f"--dest={SETTINGS_SERVICE}",
                    "/Settings",
                    "com.victronenergy.Settings.AddSetting",
                    f"string:{group}",
                    f"string:{name}",
                    f"variant:int32:{value}",
                    "string:i",
                    "variant:int32:-1",
                    "variant:int32:24",
                ],
                timeout=0.5,
            )
        if not result:
            return False
        try:
            return int(result.split()[-1]) == 0
        except ValueError:
            return False

    def ensure_tou_settings(self, default_start: int, default_end: int) -> None:
        """Create the GUI-editable TOU settings with defaults if missing.
        Existing values are never overwritten, so user edits and reinstalls
        via PackageManager both survive."""
        for path, default in (
            (TOU_START_SETTING, default_start),
            (TOU_END_SETTING, default_end),
        ):
            if self._dbus_get(SETTINGS_SERVICE, path) is not None:
                continue
            group, name = path.rstrip("/").rsplit("/", 1)
            group = group.removeprefix("/Settings/")
            if self._dbus_add_setting(group, name, default):
                logger.info("Created TOU setting %s = %d", path, default)

    def _local_today(self) -> int:
        """Day-of-year in the user's timezone (/Settings/System/TimeZone).
        Falls back to system localtime (UTC on Venus) if setting is missing."""
        return self._local_now().timetuple().tm_yday

    def _poll_battery_daily_energy(self):
        """Integrate battery power over time into daily charge/discharge kWh (5s tick).
        com.victronenergy.system has no /History/Daily paths on dbus-systemcalc-py
        systems, so we accumulate bp ourselves and reset at midnight."""
        now = time.time()
        if now - self._last_battery_daily_energy_time < 5.0:
            return

        today = self._local_today()
        if today != self._battery_energy_date:
            # Promote today's totals to "yesterday" before resetting at midnight
            self._battery_energy_yesterday = self._cached_battery_daily_energy
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
                        "y_charge": self._battery_energy_yesterday[0],
                        "y_discharge": self._battery_energy_yesterday[1],
                    },
                    f,
                )
        except OSError as e:
            logger.debug("Battery energy persist failed: %s", e)

    def _load_battery_daily_energy(self):
        """Load battery daily energy accumulators from a previous run.
        Same-day file restores today's totals; a file stamped with yesterday's
        date is promoted to the 'yesterday' slot (restart across midnight)."""
        try:
            with open(self._battery_energy_file, encoding="utf-8") as f:
                data = json.load(f)
            charge = float(data.get("charge", 0.0))
            discharge = float(data.get("discharge", 0.0))
            date = int(data.get("date", 0))
            today = self._local_today()
            if date == today:
                self._cached_battery_daily_energy = (charge, discharge)
                self._battery_energy_date = date
                self._battery_energy_yesterday = (
                    float(data.get("y_charge", 0.0)),
                    float(data.get("y_discharge", 0.0)),
                )
            elif date == (self._local_now() - timedelta(days=1)).timetuple().tm_yday:
                self._battery_energy_yesterday = (charge, discharge)
                self._battery_energy_date = today  # skip rollover (would zero yesterday)
        except (OSError, ValueError, TypeError):
            pass  # Missing or corrupt file -> start from zero

    @property
    def vebus_service(self) -> str | None:
        return self._vebus_service

    @property
    def mppt_services(self) -> list:
        return self._mppt_services

    def _safe_subprocess(self, cmd: list, timeout: float = 0.5) -> str | None:
        """Run subprocess with strict timeout and error handling"""
        self.subprocess_calls += 1
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

    def _safe_subprocess_tracked(self, cmd: list, service: str, timeout: float = 0.5) -> str | None:
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

    def dbus_get(self, service: str, path: str) -> str | None:
        """Public single-value read (native connection, CLI fallback)."""
        return self._dbus_get(service, path)

    def _dbus_get(self, service: str, path: str) -> str | None:
        """Get a single value from D-Bus (native connection, CLI fallback).
        Skips known-unresponsive services to avoid blocking the caller."""
        if not self._service_healthy(service):
            return None

        if self._native is not None:
            value = self._native.get_value(service, path)
            if value is not None:
                self._consecutive_errors = 0
                self._last_success_time = time.time()
                self._record_service_success(service)
                return value
            logger.debug(
                "D-Bus native read failed, falling back to dbus-send: %s %s", service, path
            )

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
                timeout=0.5,
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

    def _dbus_get_native_only(self, service: str, path: str) -> str | None:
        """Best-effort native read with NO dbus-send fallback.

        For optional, display-only paths (e.g. /TimeToGo on the mqtt-chain and
        virtual batteries that simply do not export it): native is a full bus
        client, so if it returns ``None`` the path is absent. Spawning a
        dbus-send subprocess that also fails (0.5 s x N under _dbus_lock) is
        pure waste — this was the per-cycle source of the update_state tail
        spikes observed on 2026-08-27."""
        if self._native is not None and self._service_healthy(service):
            return self._native.get_value(service, path)
        return None

    def _dbus_set(self, service: str, path: str, value: int, value_type: str = "int16") -> bool:
        """Set a value on D-Bus (native connection, CLI fallback).
        Uses _set_lock so writes never wait behind telemetry reads."""

        if self._native is not None:
            with self._set_lock:
                ok = self._native.set_value(service, path, value, value_type)
            if ok:
                self._consecutive_errors = 0
                self._last_success_time = time.time()
                return True
            else:
                logger.warning(
                    f"Native D-Bus set failed: service={service}, path={path}, value={value}, type={value_type}"
                )
                logger.debug(
                    "Native D-Bus set failed, falling back to dbus-send: %s %s",
                    service,
                    path,
                )

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
                timeout=0.5,
            )
            if result is not None:
                self._consecutive_errors = 0
                self._last_success_time = time.time()
                return True

            logger.warning(
                f"D-Bus set failed (fallback): service={service}, path={path}, value={value}, type={value_type}"
            )
            self._consecutive_errors += 1
            return False

    def get_system_data(self) -> dict[str, Any]:
        """
        Get all system data - now returns instantly from background-poll cache.
        """
        # Return cached data from background polling. While signal-driven,
        # values are current by construction even without recent changes.
        cache_fresh = self._system_data and (
            time.time() - self._system_data.get("_last_update", 0) < 1.0
            or (self._signals_healthy() and self._system_data.get("_last_update", 0) > 0)
        )
        if cache_fresh:
            # The signal path can update _system_data one key at a time, so on
            # the very first cycle the cached dict may hold only a subset of
            # keys. Merge over full defaults so hot-path consumers (which index
            # g1/g2/t1/t2/gt/tt/bv/bc/bp directly) never KeyError on startup.
            data = dict(self._system_data)
            for k in _SYSTEM_DATA_KEYS:
                data.setdefault(k, 0)
            return data

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

        # Bank V/I/P from the SmartShunt only (see SHUNT_SIGNAL_PATHS note).
        if self._shunt_service:
            shunt_output = self._safe_subprocess(
                [
                    "dbus-send",
                    "--system",
                    "--print-reply",
                    f"--dest={self._shunt_service}",
                    "/",
                    GET_VALUE_METHOD,
                ],
                timeout=0.5,
            )
            if shunt_output:
                data.update(parse_shunt_data_output(shunt_output))
        return data

    def get_inverter_state(self) -> tuple[int, str]:
        """Get inverter state code and description - pure background-cache read.

        The 5Hz poll thread refreshes _cached_inverter_state every pass via
        _poll_inverter_state. A main-thread read here would contend with the
        poll on the same native bus (a per-2s source of update_state tail
        spikes); the only synchronous read is a one-shot on startup before the
        poll thread has populated the cache."""
        if self._last_inverter_state_time > 0:
            return self._cached_inverter_state

        if not self._vebus_service:
            self._cached_inverter_state = (0, "Unknown")
            self._last_inverter_state_time = time.time()
            return self._cached_inverter_state

        val = self._dbus_get_native_only(self._vebus_service, VEBUS_STATE_PATH)
        if val:
            try:
                self._cached_inverter_state = self._parse_inverter_state_code(val)
            except (ValueError, TypeError):
                pass
        self._last_inverter_state_time = time.time()
        return self._cached_inverter_state

    def get_battery_soc_local(self, sys_data: dict[str, Any] | None = None) -> float:
        """
        Calculate battery SOC locally from D-Bus voltage (HA "Battery %" paradigm).
        Replaces the shunt's own SoC, which reads bogus 100% while charging.
        Returns SOC percentage (0-100).
        """
        if sys_data is None:
            sys_data = self.get_system_data()
        voltage = sys_data.get("bv", 0.0)

        return calculate_battery_soc_from_voltage(voltage)

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

    def get_mppt_data(self) -> dict[str, dict[str, float]]:
        """Get power and current from all MPPT chargers - pure background-cache read.

        The 5Hz poll thread refreshes via _reconcile_groups_if_stale every
        GROUP_CACHE_TTL pass. A main-thread reconcile here would contend with
        the poll on the same native bus, which was a source of the
        calculate_setpoint tail spikes. The only synchronous reconcile is a
        one-shot on startup before the poll thread first populates the map."""
        if self._last_mppt_time > 0:
            return self._cached_mppt_data
        if not self._mppt_services:
            return {}
        self._reconcile_mppt_data()
        return self._cached_mppt_data

    def get_pv_power(self) -> list:
        """Get power from PV inverters (Tasmota, ESPHome, etc.) - pure cache."""
        if self._last_pv_time > 0:
            return self._cached_pv_powers
        if not self._pv_inverter_services:
            return []
        self._reconcile_pv_power()
        return self._cached_pv_powers

    def get_acload_powers(self) -> dict[str, float]:
        """Get power from Emporia Vue channels (acload services) - pure cache.

        Reconcile happens only in the background poll thread; this is a read
        of the per-service cache so the control loop never blocks on D-Bus."""
        if not self._acload_services:
            return {}
        if self._last_acload_time == 0:
            self._reconcile_acload_power()
        # Compose on read: signals update the per-service layer directly.
        return self._compose_acload_powers()

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
        """Get SoC for each battery chain - pure background-cache read.

        The 5Hz poll thread refreshes _cached_battery_chain_socs every pass
        (_poll_battery_chain_socs); a main-thread read here is redundant and
        contended with the poll on the same native bus, which was a per-2s
        source of the update_state tail spikes. The only synchronous read is a
        one-shot on startup before the poll thread has populated the cache."""
        if self._last_battery_chain_soc_time > 0:
            return self._cached_battery_chain_socs

        socs = self._query_battery_chain_socs()
        self._cached_battery_chain_socs = socs
        self._last_battery_chain_soc_time = time.time()
        return socs

    def get_cell_counts(self) -> dict[str, int]:
        """Get discovered cell counts per battery chain service."""
        return dict(self._chain_cell_counts)

    def get_ess_mode(self) -> dict[str, Any]:
        """Get current ESS mode - pure background-cache read.

        The 5Hz poll thread refreshes via _reconcile_ess_mode every 5s; a
        main-thread native read here contended with the poll on the same bus,
        contributing to the update_state tail spikes. The only synchronous
        read is a one-shot on startup."""
        with self._dbus_lock:
            if self._ess_mode_cache is not None and self._ess_mode_cache_time > 0:
                return dict(self._ess_mode_cache)
        self._reconcile_ess_mode()
        with self._dbus_lock:
            return dict(self._ess_mode_cache)

    def _reconcile_ess_mode(self) -> None:
        """Refresh the ESS mode cache from settings (poll thread only)."""
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
            self._ess_mode_cache_time = time.time()

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
        """Read a D-Bus path as float for the background thread.

        Native connection first (lock-free pipelined call); the dbus-send
        subprocess fallback keeps working when the native client is down."""
        if self._native is not None and self._service_healthy(service):
            val = self._native.get_value(service, path)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return 0.0
            logger.debug(
                "D-Bus native read failed, falling back to dbus-send: %s %s", service, path
            )
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
            timeout=0.5,
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
        """Get detailed data for all battery chains - pure background-cache read.

        The 5Hz poll thread refreshes via _reconcile_all_batteries every 2s;
        querying services in parallel on the main thread caused multi-hundred-ms
        update_state tail spikes. The only synchronous query is a one-shot on
        startup before the poll thread first populates the cache."""
        if self._cached_all_batteries:
            return self._cached_all_batteries
        self._reconcile_all_batteries()
        return self._cached_all_batteries

    def _reconcile_all_batteries(self) -> None:
        """Refresh all-battery-chain detail (poll thread only)."""
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
            # Native-only: these chain/virtual battery services do not export
            # /TimeToGo, so a dbus-send fallback would fail too — but at 0.5s x
            # chain under _dbus_lock, causing the update_state tail spikes.
            ttg_raw = self._dbus_get_native_only(service, "/TimeToGo")
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
        self._last_all_batteries_time = time.time()

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
        if not self._mppt_services:
            return self._cached_mppt_chargers
        if self._cached_mppt_chargers and now - self._last_mppt_chargers_time < 2.0:
            return self._cached_mppt_chargers

        def _query_mppt_charger(i: int, service: str) -> dict[str, Any]:
            parts = service.split(":")
            name = f"MPPT-{parts[1]}" if len(parts) > 1 else f"MPPT-{i}"
            return {
                "name": name,
                "pv_voltage": self._get_float(service, "/Pv/V"),
                "current": self._get_float(service, DC_CURRENT_PATH),
                "power": self._get_float(service, YIELD_POWER_PATH),
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
        return list(self._cached_pv_inverter_daily_yields)

    def get_mppt_yesterday_yields(self) -> list[float]:
        """Get yesterday's yield (kWh) for each MPPT charger - instant from background cache"""
        return list(self._cached_mppt_yesterday_yields)

    def get_pv_inverter_yesterday_yields(self) -> list[float]:
        """Get yesterday's yield (kWh) for each Tasmota PV inverter - instant from cache"""
        return list(self._cached_pv_inverter_yesterday_yields)

    def get_battery_daily_energy(self) -> tuple[float, float]:
        """Get battery daily charge/discharge energy (kWh) - instant from background cache"""
        return self._cached_battery_daily_energy

    def get_battery_yesterday_energy(self) -> tuple[float, float]:
        """Get yesterday's battery charge/discharge energy (kWh) - instant from cache.
        Promoted from today's totals at the local-midnight rollover."""
        return self._battery_energy_yesterday

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

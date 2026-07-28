#!/usr/bin/env python3
"""
Inverter Control - Main Entry Point
Grid-zero feed-in control for Victron system with split-phase compensation
"""

import argparse
import atexit
import gc
import logging
import os
import signal
import sys
import threading
import time
import traceback
from typing import Any

from inverter_control.config import (
    BURST_GAIN,
    BURST_THRESHOLD,
    CREEP_MAX,
    CREEP_RATE,
    D_BRAKE_ZONE,
    D_GAIN,
    D_THRESHOLD,
    DAMPING_FACTOR,
    DRY_RUN,
    DVCC_CCL_CHANGE_RATE,
    DVCC_CELL_BALANCE_VOLTAGE,
    DVCC_CELL_CUTOFF,
    DVCC_CELL_FULL_CURRENT,
    DVCC_CELL_MAX_VOLTAGE,
    DVCC_CELL_NEAR_FULL,
    DVCC_CELL_START_LIMIT,
    DVCC_DCL_CHANGE_RATE,
    DVCC_ENABLED,
    DVCC_IMBALANCE_AGGRESSIVE,
    DVCC_IMBALANCE_CRITICAL,
    DVCC_IMBALANCE_START_LIMIT,
    DVCC_MAX_CHARGE_CURRENT,
    DVCC_MAX_DISCHARGE_CURRENT,
    DVCC_MIN_CHARGE_CURRENT,
    DVCC_SOC_DISCHARGE_REDUCED,
    DVCC_SOC_DISCHARGE_STOP,
    DVCC_SOC_REDUCE_FACTOR,
    DVCC_SOC_REDUCE_START,
    DVCC_TEMP_DISCHARGE_MIN,
    DVCC_TEMP_DISCHARGE_REDUCED,
    DVCC_TEMP_FULL_CURRENT_MAX,
    DVCC_TEMP_FULL_CURRENT_MIN,
    DVCC_TEMP_REDUCED,
    DVCC_TEMP_STOP_CHARGE,
    DVCC_TEMP_STOP_CHARGE_HIGH,
    EMA_ALPHA,
    ENABLE_EV,
    ENABLE_HA,
    ENABLE_HA_LOADS,
    ENABLE_WATER,
    EXPORT_DAMPING,
    GRID_ZERO_DEADBAND_HIGH,
    GRID_ZERO_DEADBAND_LOW,
    INVERTER_EFFICIENCY,
    LOOP_INTERVAL,
    MQTT_SLIM_EXCLUDE_KEYS,
    MQTT_SLIM_STATE,
    POWER_LIMIT_MAX,
    POWER_LIMIT_MIN,
    SETPOINT_DELTA_LIMIT,
    SOLAR_OUTPUT_OFFSET,
)
from inverter_control.config import (
    Colors as C,
)
from inverter_control.console_server import (
    broadcast_line,
)
from inverter_control.console_server import (
    start_server as start_console_server,
)
from inverter_control.console_server import (
    stop_server as stop_console_server,
)
from inverter_control.console_ui import ConsoleUI
from inverter_control.dvcc import DvccLimits, create_dvcc_from_config
from inverter_control.homeassistant import get_ha
from inverter_control.logic import SetpointCalculator, SystemState
from inverter_control.victron import get_victron

try:
    from inverter_control.mqtt_bridge import MQTT_AVAILABLE, get_mqtt_bridge
except ImportError:
    MQTT_AVAILABLE = False

    def get_mqtt_bridge(*_args, **_kwargs):
        return None


# =============================================================================
# LOGGING SETUP - All errors go to file
# =============================================================================
LOG_FILE = os.environ.get("INVERTER_CONTROL_LOG_FILE", "/var/log/inverter-control.log")

logger = logging.getLogger("inverter-control")
logger.setLevel(logging.DEBUG)

try:
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.INFO)
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(fh)
except Exception as log_err:
    print(f"Warning: Could not create log file: {log_err}", file=sys.stderr)


def log_exception(msg: str):
    """Log exception with full traceback"""
    logger.error(f"{msg}\n{traceback.format_exc()}")


def get_version() -> str:
    """Read version from version file"""
    try:
        version_file = os.path.join(os.path.dirname(__file__), "version")
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "unknown"


VERSION = get_version()


class WatchdogTimeoutError(Exception):
    """Raised when a watchdog timeout occurs"""


class HardwareWatchdog:
    """
    Hardware watchdog for Victron ESS setpoint safety.

    Monitors telemetry freshness (D-Bus + MQTT). If no updates for timeout seconds,
    forces ESS setpoint to 0W (pass-through/fallback mode) to prevent uncontrolled
    grid export/import if the control loop stalls or crashes.

    Runs as a daemon thread checking heartbeat every second.
    """

    def __init__(
        self,
        victron,
        timeout_seconds: int = 30,
        check_interval: float = 1.0,
        dry_run: bool = False,
        get_setpoint=None,
    ):
        self.victron = victron
        self.timeout_seconds = timeout_seconds
        self.check_interval = check_interval
        self.dry_run = dry_run
        # Optional callback returning the last-applied grid setpoint, so it
        # can be restored (instead of assuming external mode) after recovery.
        self._get_setpoint = get_setpoint
        self._last_dbus_update = 0.0
        self._last_mqtt_update = 0.0
        self._enabled = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_external: bool | None = None
        self._pre_forced_setpoint: int = 0
        self._lock = threading.Lock()

    def mark_dbus_update(self):
        """Call when D-Bus telemetry is successfully read"""
        with self._lock:
            self._last_dbus_update = time.time()

    def mark_mqtt_update(self):
        """Call when MQTT state is successfully published"""
        with self._lock:
            self._last_mqtt_update = time.time()

    def start(self):
        """Start the watchdog monitoring thread"""
        if self._enabled:
            return
        self._enabled = True
        self._stop_event.clear()
        self._triggered = False
        self._hardware_forced = False
        self._pre_forced_external = None
        self._pre_forced_setpoint = 0
        now = time.time()
        self._last_dbus_update = now
        self._last_mqtt_update = now
        self._thread = threading.Thread(target=self._run, name="hardware-watchdog", daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the watchdog thread"""
        self._enabled = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self):
        """Main watchdog loop - checks heartbeat every interval"""
        while not self._stop_event.wait(self.check_interval):
            if not self._enabled:
                break
            self._check_heartbeat()

    def _check_heartbeat(self):
        """Check if telemetry is stale and trigger failsafe if needed"""
        now = time.time()
        with self._lock:
            dbus_age = now - self._last_dbus_update
            mqtt_age = now - self._last_mqtt_update

        # Trigger if BOTH D-Bus and MQTT are stale (control loop stopped)
        # If at least one is fresh, the system is still alive
        stale = dbus_age > self.timeout_seconds and mqtt_age > self.timeout_seconds

        if stale and not self._triggered:
            self._triggered = True
            self._apply_failsafe()
        elif stale and self._triggered and not self.dry_run and not self._hardware_forced:
            # Latched while in dry-run (or hardware action previously failed) but now
            # live - still stale, so apply the live failsafe action now.
            self._apply_failsafe()
        elif not stale and self._triggered:
            self._recover_from_failsafe()

    def _apply_failsafe(self):
        """Force ESS into safe pass-through mode, remembering prior state for recovery"""
        if self.dry_run:
            logger.warning("[DRY] watchdog would force 0W setpoint / ESS pass-through")
            return
        try:
            if self._pre_forced_external is None:
                # Remember prior ESS mode/setpoint so we can restore them on recovery
                self._pre_forced_external = self.victron.get_ess_mode().get("is_external")
                self._pre_forced_setpoint = self._get_setpoint() if self._get_setpoint else 0
            # Force ESS to pass-through mode (0W setpoint = fallback)
            self.victron.set_grid_setpoint(0)
            # Also force external control mode off for safety
            self.victron.set_ess_mode(external=False)
            self._hardware_forced = True
        except Exception:
            pass  # Best effort - don't crash watchdog

    def _recover_from_failsafe(self):
        """Telemetry recovered - re-arm watchdog and restore prior ESS mode/setpoint"""
        self._triggered = False
        if self._hardware_forced:
            try:
                if self._pre_forced_external:
                    self.victron.set_ess_mode(external=True)
                    self.victron.set_grid_setpoint(self._pre_forced_setpoint)
            except Exception:
                pass  # Best effort - don't crash watchdog
            self._hardware_forced = False
        self._pre_forced_external = None
        self._pre_forced_setpoint = 0
        logger.info("hardware watchdog re-armed after telemetry recovery")

    def is_triggered(self) -> bool:
        """Return True if watchdog has triggered failsafe"""
        return self._triggered

    def get_status(self) -> dict:
        """Return watchdog status for diagnostics"""
        now = time.time()
        with self._lock:
            return {
                "enabled": self._enabled,
                "triggered": self._triggered,
                "timeout_seconds": self.timeout_seconds,
                "dbus_age_seconds": round(now - self._last_dbus_update, 1),
                "mqtt_age_seconds": round(now - self._last_mqtt_update, 1),
            }


# =============================================================================
# INVERTER CONTROLLER
# =============================================================================


class InverterController:
    """
    Main controller for grid-zero feed-in management.
    Coordinates I/O (D-Bus, HA) and delegates logic to SetpointCalculator.
    """

    def __init__(self, dry_run: bool | None = None):
        self._start_time = time.time()
        self.dry_run = dry_run if dry_run is not None else DRY_RUN
        self.victron = get_victron()
        self.ha = get_ha()

        # Load UI configuration
        from inverter_control.ui_config import (
            get_ui_config,  # pylint: disable=import-outside-toplevel
        )

        self.ui_config = get_ui_config()

        # Initialize Logic and UI components
        config_dict = {
            "EMA_ALPHA": EMA_ALPHA,
            "POWER_LIMIT_MIN": POWER_LIMIT_MIN,
            "POWER_LIMIT_MAX": POWER_LIMIT_MAX,
            "SETPOINT_DELTA_LIMIT": SETPOINT_DELTA_LIMIT,
            "DAMPING_FACTOR": DAMPING_FACTOR,
            "GRID_ZERO_DEADBAND_LOW": GRID_ZERO_DEADBAND_LOW,
            "GRID_ZERO_DEADBAND_HIGH": GRID_ZERO_DEADBAND_HIGH,
            "INVERTER_EFFICIENCY": INVERTER_EFFICIENCY,
            "SOLAR_OUTPUT_OFFSET": SOLAR_OUTPUT_OFFSET,
            "CREEP_RATE": CREEP_RATE,
            "CREEP_MAX": CREEP_MAX,
            "EXPORT_DAMPING": EXPORT_DAMPING,
            "BURST_THRESHOLD": BURST_THRESHOLD,
            "BURST_GAIN": BURST_GAIN,
            "D_BRAKE_ZONE": D_BRAKE_ZONE,
            "D_THRESHOLD": D_THRESHOLD,
            "D_GAIN": D_GAIN,
        }
        self.calculator = SetpointCalculator(config_dict)
        self.console = ConsoleUI(self.ha, self.victron)

        # State
        self.current_setpoint = 0
        self.previous_setpoint = 0
        self.manual_setpoint: int | None = None
        self.delay = 0  # Delay counter for load switching
        self.filtered_gt: float | None = None

        self.loop_count = 0
        self.state: dict[str, Any] = {}

        # Cached D-Bus data
        self._cached_mppt_data = {}
        self._cached_tasmota_powers = []
        self._cached_battery_socs = []
        self._cached_inv_state = ""

        # Dynamic settings (overridable)
        self.power_limit_min = POWER_LIMIT_MIN
        self.power_limit_max = POWER_LIMIT_MAX
        self.loop_interval = LOOP_INTERVAL

        # DVCC Calculator for dynamic battery current limits (SoC & Cell Temp curves)
        if DVCC_ENABLED:
            self.dvcc_calculator = create_dvcc_from_config(
                {
                    "DVCC_CELL_COUNT": 16,  # Will be updated from actual battery data
                    "DVCC_MAX_CHARGE_CURRENT": DVCC_MAX_CHARGE_CURRENT,
                    "DVCC_MAX_DISCHARGE_CURRENT": DVCC_MAX_DISCHARGE_CURRENT,
                    "DVCC_CELL_MAX_VOLTAGE": DVCC_CELL_MAX_VOLTAGE,
                    "DVCC_CELL_START_LIMIT": DVCC_CELL_START_LIMIT,
                    "DVCC_CELL_BALANCE_VOLTAGE": DVCC_CELL_BALANCE_VOLTAGE,
                    "DVCC_CCL_CHANGE_RATE": DVCC_CCL_CHANGE_RATE,
                    "DVCC_DCL_CHANGE_RATE": DVCC_DCL_CHANGE_RATE,
                    "DVCC_CELL_FULL_CURRENT": DVCC_CELL_FULL_CURRENT,
                    "DVCC_CELL_NEAR_FULL": DVCC_CELL_NEAR_FULL,
                    "DVCC_CELL_CUTOFF": DVCC_CELL_CUTOFF,
                    "DVCC_MIN_CHARGE_CURRENT": DVCC_MIN_CHARGE_CURRENT,
                    "DVCC_IMBALANCE_START_LIMIT": DVCC_IMBALANCE_START_LIMIT,
                    "DVCC_IMBALANCE_AGGRESSIVE": DVCC_IMBALANCE_AGGRESSIVE,
                    "DVCC_IMBALANCE_CRITICAL": DVCC_IMBALANCE_CRITICAL,
                    "DVCC_TEMP_STOP_CHARGE": DVCC_TEMP_STOP_CHARGE,
                    "DVCC_TEMP_REDUCED": DVCC_TEMP_REDUCED,
                    "DVCC_TEMP_FULL_CURRENT_MIN": DVCC_TEMP_FULL_CURRENT_MIN,
                    "DVCC_TEMP_FULL_CURRENT_MAX": DVCC_TEMP_FULL_CURRENT_MAX,
                    "DVCC_TEMP_STOP_CHARGE_HIGH": DVCC_TEMP_STOP_CHARGE_HIGH,
                    "DVCC_TEMP_DISCHARGE_MIN": DVCC_TEMP_DISCHARGE_MIN,
                    "DVCC_TEMP_DISCHARGE_REDUCED": DVCC_TEMP_DISCHARGE_REDUCED,
                    "DVCC_SOC_REDUCE_START": DVCC_SOC_REDUCE_START,
                    "DVCC_SOC_REDUCE_FACTOR": DVCC_SOC_REDUCE_FACTOR,
                    "DVCC_SOC_DISCHARGE_STOP": DVCC_SOC_DISCHARGE_STOP,
                    "DVCC_SOC_DISCHARGE_REDUCED": DVCC_SOC_DISCHARGE_REDUCED,
                }
            )
            self.dvcc_limits: DvccLimits | None = None
        else:
            self.dvcc_calculator = None
            self.dvcc_limits = None

        # Hardware watchdog - triggers fallback if telemetry stops.
        # Started explicitly in _run_main_loop (not here) to avoid triggering
        # during a slow startup sequence.
        self._watchdog = HardwareWatchdog(
            victron=self.victron,
            timeout_seconds=30,
            check_interval=5.0,
            dry_run=self.dry_run,
            get_setpoint=lambda: self.previous_setpoint,
        )

    def set_loop_interval(self, interval: float) -> float:
        self.loop_interval = max(0.1, min(5.0, interval))
        logger.info(f"Loop interval changed to {self.loop_interval}s")
        return self.loop_interval

    def set_power_limits(self, min_val: int, max_val: int) -> dict[str, int]:
        self.power_limit_min = max(min_val, -3000)
        self.power_limit_max = min(max_val, 3000)
        # Update calculator limits
        self.calculator.power_limit_min = self.power_limit_min
        self.calculator.power_limit_max = self.power_limit_max
        logger.info(f"Power limits changed to [{self.power_limit_min}, {self.power_limit_max}]")
        return {"min": self.power_limit_min, "max": self.power_limit_max}

    def toggle_dry_run(self) -> bool:
        self.dry_run = not self.dry_run
        self._watchdog.dry_run = self.dry_run
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(f"Mode changed to {mode}")
        return self.dry_run

    def toggle_ess_mode(self) -> dict[str, Any]:
        current = self.victron.get_ess_mode()
        new_external = not current["is_external"]
        if self.victron.set_ess_mode(external=new_external):
            new_mode = self.victron.get_ess_mode()
            logger.info(f"ESS Mode changed to {new_mode['mode_name']}")
            return new_mode
        return current

    def get_state(self) -> dict[str, Any]:
        return self.state

    def set_manual_setpoint(self, value: int) -> bool:
        self.manual_setpoint = max(self.power_limit_min, min(self.power_limit_max, value))
        return True

    def calculate_setpoint(self, sys_data: dict[str, Any]) -> tuple[int, str]:
        """Orchestrate state collection and delegate calculation to logic.py"""

        # Prepare SystemState snapshot
        mppt_data = self.victron.get_mppt_data()
        mppt_total = sum(m["w"] for m in mppt_data.values())
        tasmota_powers = self.victron.get_tasmota_pv_power()
        tasmota_total = sum(tasmota_powers)

        state = SystemState(
            g1=sys_data["g1"],
            g2=sys_data["g2"],
            gt=sys_data["gt"],
            t1=sys_data["t1"],
            t2=sys_data["t2"],
            tt=sys_data["tt"],
            inv_power=self.victron.get_inverter_power(),
            mppt_total=mppt_total,
            tasmota_total=tasmota_total,
            pv_total=mppt_total + tasmota_total,
            ev_power=self.ha.get_vue_sensor("ev_charger", 0),
            garage_power=self.ha.get_vue_sensor("garage", 0),
            only_charging=self.ha.get_boolean("only_charging"),
            no_feed=self.ha.get_boolean("no_feed"),
            house_support=self.ha.get_boolean("house_support"),
            charge_battery=self.ha.get_boolean("charge_battery"),
            do_not_supply_charger=self.ha.get_boolean("do_not_supply_charger"),
            limit_to_ev=self.ha.get_boolean("set_limit_to_ev_charger"),
            previous_setpoint=self.previous_setpoint,
            filtered_gt=self.filtered_gt,
        )

        # Perform calculation
        result = self.calculator.calculate(state)

        # Update persistence
        self.filtered_gt = result.filtered_gt

        return result.setpoint, result.flags

    def handle_minimize_charging(self, sys_data: dict[str, Any]):
        try:
            if self.delay > 0:
                self.delay -= 1
                return
            if not self.ha.get_boolean("minimize_charging"):
                return
            inverter_state, _ = self.victron.get_inverter_state()
            if inverter_state == 0:
                return
            net_usage = self.ha.get_sensor("net_usage", 0)
            bp = sys_data.get("bp", 0)
            if 0 < net_usage < 200 and bp > 750:
                changed = self.ha.control_dump_loads(turn_on=True)
                if changed > 0:
                    self.delay = 6
                    print(f" [MC+{changed}] ", end="")
            elif bp < -650 or net_usage > 650:
                changed = self.ha.control_dump_loads(turn_on=False)
                if changed > 0:
                    self.delay = 6
                    print(f" [MC-{changed}] ", end="")
        except Exception as e:
            logger.warning(f"minimize_charging error: {e}")

    def _get_ev_state(self) -> dict[str, Any]:
        if not ENABLE_EV:
            return {"ev_power": 0, "car_soc": 0, "ev_charging_kw": 0}
        return {
            "ev_power": self.ha.get_vue_sensor("ev_charger", 0),
            "car_soc": self.ha.get_sensor("car_soc", 0),
            "ev_charging_kw": self.ha.get_sensor("ev_charging_power", 0),
        }

    def _get_water_state(self) -> dict[str, Any]:
        if not ENABLE_WATER:
            return {"water_level": 0, "water_valve": False, "pump_switch": False}
        return {
            "water_level": self.ha.get_sensor("water_level", 0),
            "water_valve": self.ha.water_valve_on,
            "pump_switch": self.ha.pump_switch_on,
        }

    def _get_ha_state(self) -> dict[str, Any]:
        if not ENABLE_HA:
            return {
                "booleans": {},
                "laundry_outlet": False,
                "home_recliner": False,
                "home_garage": False,
                "ha_connected": False,
                "ha_uptime": 0,
            }
        return {
            "booleans": self.ha.get_all_booleans(),
            "laundry_outlet": self.ha.laundry_outlet_on,
            "home_recliner": self.ha.home_recliner_on,
            "home_garage": self.ha.home_garage_on,
            "ha_connected": self.ha.connected,
            "ha_uptime": self.ha.uptime,
        }

    def _get_daily_stats(self) -> dict[str, Any]:
        if not ENABLE_HA:
            return {}
        return {
            "produced_today": self.ha.get_sensor("produced_today", 0),
            "produced_dollars": self.ha.get_sensor("produced_dollars", 0),
            "grid_kwh": self.ha.get_sensor("grid_kwh_today", 0),
            "battery_in": self.ha.get_sensor("battery_in_today", 0),
            "battery_out": self.ha.get_sensor("battery_out_today", 0),
            "battery_in_yesterday": self.ha.get_sensor("battery_in_yesterday", 0),
            "battery_out_yesterday": self.ha.get_sensor("battery_out_yesterday", 0),
            "pv_total_daily": self.ha.get_sensor("pv_total_daily", 0),
            "tasmota_daily": [
                self.ha.get_sensor("tasmota_1_daily", 0),
                self.ha.get_sensor("tasmota_2_daily", 0),
            ],
            "mppt_daily": [
                self.ha.get_sensor("mppt_1_daily", 0),
                self.ha.get_sensor("mppt_2_daily", 0),
                self.ha.get_sensor("mppt_3_daily", 0),
            ],
        }

    def update_state(self, sys_data: dict[str, Any], setpoint: int):
        self._cached_mppt_data = self.victron.get_mppt_data()
        self._cached_tasmota_powers = self.victron.get_tasmota_pv_power()
        self._cached_battery_socs = self.victron.get_battery_chain_socs()
        _, self._cached_inv_state = self.victron.get_inverter_state()

        # Inject cached data into sys_data for console UI use
        sys_data["mppt_data"] = self._cached_mppt_data
        sys_data["tasmota_powers"] = self._cached_tasmota_powers
        sys_data["battery_socs"] = self._cached_battery_socs

        # Full state for web UI
        self.state = {
            **sys_data,
            "setpoint": setpoint,
            "filtered_gt": self.filtered_gt,
            "dry_run": self.dry_run,
            "mppt_total": sum(m["w"] for m in self._cached_mppt_data.values()),
            "tasmota_total": sum(self._cached_tasmota_powers),
            "solar_total": sum(m["w"] for m in self._cached_mppt_data.values())
            + sum(self._cached_tasmota_powers),
            "mppt_data": self._cached_mppt_data,
            "mppt_individual": [m["w"] for m in self._cached_mppt_data.values()],
            "tasmota_individual": self._cached_tasmota_powers,
            "inverter_state": self._cached_inv_state,
            "battery_socs": self._cached_battery_socs,
            "batteries": self.victron.get_all_batteries(),
            "mppt_chargers": self.victron.get_mppt_chargers(),
            **self._get_ev_state(),
            **self._get_water_state(),
            **self._get_ha_state(),
            "loads": self.ha.get_all_vue_sensors() if ENABLE_HA_LOADS else {},
            "ess_mode": self.victron.get_ess_mode(),
            "battery_power": sys_data.get("bp", 0),
            "battery_voltage": sys_data.get("bv", 0),
            "battery_current": sys_data.get("bc", 0),
            "battery_soc": sys_data.get("soc", 0) or self.ha.get_sensor("corrected_soc", 0),
            "daily_stats": self._get_daily_stats(),
            "limits": {"min": self.power_limit_min, "max": self.power_limit_max},
            "loop_interval": self.loop_interval,
            "version": VERSION,
            "uptime": int(time.time() - self._start_time),
            "ui_config": self.ui_config,
            "dvcc_limits": self.dvcc_limits.__dict__ if self.dvcc_limits else None,
        }

    def get_state_for_mqtt(self) -> dict[str, Any]:
        if not MQTT_SLIM_STATE:
            return self.state
        out = dict(self.state)
        for k in MQTT_SLIM_EXCLUDE_KEYS:
            out.pop(k, None)
        return out

    def run_cycle(self) -> bool:
        def watchdog_handler(signum, frame):
            raise WatchdogTimeoutError("Control cycle watchdog timeout")

        old_handler = signal.signal(signal.SIGALRM, watchdog_handler)
        signal.alarm(5)
        try:
            sys_data = self.victron.get_system_data()
            # Mark D-Bus telemetry as fresh for hardware watchdog
            self._watchdog.mark_dbus_update()

            if self.dvcc_calculator is not None:
                battery_data = self.victron.get_battery_cell_data()
                self.dvcc_limits = self.dvcc_calculator.calculate(battery_data)
            if self.manual_setpoint is not None:
                setpoint = self.manual_setpoint
                flags = "[MANUAL] "
                self.manual_setpoint = None
            else:
                setpoint, flags = self.calculate_setpoint(sys_data)

            self.handle_minimize_charging(sys_data)
            if self.dry_run:
                flags = f"{C.MAGENTA}[DRY]{C.RESET}" + flags
            else:
                self.victron.set_grid_setpoint(setpoint)

            print(f"\033k{sys_data['gt']}\033\\", end="")

            # Inject cached data for console UI
            sys_data["battery_socs"] = self._cached_battery_socs
            sys_data["mppt_data"] = self._cached_mppt_data
            sys_data["tasmota_powers"] = self._cached_tasmota_powers

            filtered_display = self.filtered_gt if self.filtered_gt is not None else sys_data["gt"]
            line = self.console.format_line(
                sys_data, setpoint, self.previous_setpoint, flags, filtered_display
            )
            print(line)
            broadcast_line(line)

            self.update_state(sys_data, setpoint)
            self.console.update_terminal_title()
            self.previous_setpoint = setpoint

            try:
                if self.ha.get_boolean("no_feed"):
                    time.sleep(2)
            except Exception:
                pass  # Best effort - ignore HA lookup failures for this optional delay
            return True
        except KeyboardInterrupt:
            return False
        except WatchdogTimeoutError:
            logger.error("WATCHDOG: Cycle timeout")
            return True
        except Exception as e:
            log_exception(f"Error in control cycle: {e}")
            return True
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)


# =============================================================================
# ENTRY POINT
# =============================================================================


def main():
    logger.info("=== Inverter Control starting ===")

    try:
        _main_inner()
    except Exception as e:
        log_exception(f"FATAL ERROR in main: {e}")
        raise


def _setup_mqtt_bridge(controller):
    """Set up MQTT bridge and register command callbacks. Returns bridge or None."""
    from inverter_control.config import (  # pylint: disable=import-outside-toplevel
        MQTT_BROKER,
        MQTT_PORT,
        MQTT_TOPIC_PREFIX,
    )

    if not MQTT_AVAILABLE or not MQTT_BROKER:
        return None

    bridge = get_mqtt_bridge(broker=MQTT_BROKER, port=MQTT_PORT, prefix=MQTT_TOPIC_PREFIX)
    if not bridge:
        return None

    bridge.connect()
    bridge.register_callback("toggle", lambda p: controller.ha.toggle_entity(p.get("entity", "")))
    bridge.register_callback("press", lambda p: controller.ha.press_button(p.get("entity", "")))
    bridge.register_callback(
        "setpoint",
        lambda p: controller.set_manual_setpoint(int(p.get("value", 0))),
    )
    bridge.register_callback("dry_run", lambda p: controller.toggle_dry_run())
    bridge.register_callback(
        "limits",
        lambda p: controller.set_power_limits(p.get("min", -2300), p.get("max", 2250)),
    )
    bridge.register_callback("ess_mode", lambda p: controller.toggle_ess_mode())
    bridge.register_callback(
        "loop_interval",
        lambda p: controller.set_loop_interval(float(p.get("interval", 0.33))),
    )
    print(f"  MQTT bridge: {MQTT_BROKER}:{MQTT_PORT} (topic: {MQTT_TOPIC_PREFIX}/)")
    return bridge


def _run_main_loop(controller, mqtt_bridge):
    """Run the main control loop until exit or error."""
    gc_interval = 300
    last_gc_time = time.time()
    # Use /run for runtime files (cleared on reboot, secure)
    heartbeat_dir = "/run/inverter-control"
    heartbeat_file = f"{heartbeat_dir}/heartbeat"

    # Start the hardware watchdog just before entering the loop, so slow
    # startup work above doesn't get mistaken for a stalled control loop.
    controller._watchdog.start()

    try:
        os.makedirs(heartbeat_dir, mode=0o755, exist_ok=True)
        while True:
            if not controller.run_cycle():
                logger.info("run_cycle returned False - exiting main loop")
                break
            if mqtt_bridge and mqtt_bridge.connected:
                mqtt_bridge.publish_state(controller.get_state_for_mqtt())
                # Mark MQTT telemetry as fresh for hardware watchdog
                controller._watchdog.mark_mqtt_update()

            # Write heartbeat for watchdog
            try:
                with open(heartbeat_file, "w", encoding="utf-8") as f:
                    f.write(str(int(time.time())))
            except OSError:
                pass  # Ignore if heartbeat fails

            now = time.time()
            if now - last_gc_time > gc_interval:
                last_gc_time = now
                gc.collect()
            time.sleep(controller.loop_interval)
    except KeyboardInterrupt:
        logger.info("Shutdown requested (KeyboardInterrupt)")
        print("\nShutting down...")
    finally:
        logger.info("Inverter Control shutting down")
        # Stop hardware watchdog
        try:
            controller._watchdog.stop()
        except Exception:
            pass  # Best effort - shutdown must proceed even if watchdog stop fails
        stop_console_server()
        if mqtt_bridge:
            mqtt_bridge.disconnect()
        controller.ha.stop()


def _main_inner():
    parser = argparse.ArgumentParser(description="Inverter Control for Victron System")
    parser.add_argument(
        "setpoint",
        type=int,
        nargs="?",
        default=None,
        help="Manual setpoint (one-shot mode)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't actually send commands")
    args = parser.parse_args()

    print(f"=== Inverter Control {VERSION} ===")

    dry_run_mode = args.dry_run or None
    controller = InverterController(dry_run=dry_run_mode)

    mode = "DRY-RUN (safe mode)" if controller.dry_run else "LIVE (sending commands)"
    print(f"Mode: {mode}")

    mqtt_bridge = _setup_mqtt_bridge(controller)

    if args.setpoint is not None:
        controller.manual_setpoint = args.setpoint
        controller.run_cycle()
        return

    start_console_server()
    print("Starting control loop...")
    print("-" * 80)

    _run_main_loop(controller, mqtt_bridge)


def signal_handler(signum, frame):
    """Log signal and exit"""
    sig_names = {
        signal.SIGTERM: "SIGTERM",
        signal.SIGINT: "SIGINT",
        signal.SIGHUP: "SIGHUP",
    }
    sig_name = sig_names.get(signum, f"signal {signum}")
    logger.warning(f"Received {sig_name} - shutting down")
    sys.exit(0)


def excepthook(exc_type, exc_value, exc_tb):
    """Log uncaught exceptions"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.error(
        f"Uncaught exception: {exc_type.__name__}: {exc_value}\n{''.join(traceback.format_tb(exc_tb))}"
    )


def exit_handler():
    """Log on normal exit"""
    logger.info("Process exiting")


if __name__ == "__main__":
    # Install handlers to track exit reasons
    sys.excepthook = excepthook
    atexit.register(exit_handler)

    # Install signal handlers to log shutdown reason
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    main()

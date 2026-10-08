"""Main controller for grid-zero feed-in management."""

import logging
import time
import traceback
from typing import Any

import inverter_control.config as _config
from inverter_control.background_reader import BackgroundReader
from inverter_control.config import (
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
    ENABLE_ACLOADS,
    ENABLE_EV,
    ENABLE_GRID_SMOOTHING_WITH_HOME,
    ENABLE_HA,
    ENABLE_WATER,
    ESS_EXTERNAL_WARN_MINUTES,
    GRID_FILTER_TAU,
    GRID_SMOOTHING_DERIVED_TAU,
    LOOP_INTERVAL,
    MQTT_SLIM_EXCLUDE_KEYS,
    MQTT_SLIM_STATE,
    NO_FEED_SLEEP_INTERVAL,
    POWER_LIMIT_MAX,
    POWER_LIMIT_MIN,
    WATCHDOG_CHECK_INTERVAL,
    WATCHDOG_TIMEOUT_SECONDS,
    WEBHOOK_SERVER_HOST,
    WEBHOOK_SERVER_PORT,
)
from inverter_control.config import (
    Colors as C,
)
from inverter_control.console_server import broadcast_line
from inverter_control.console_ui import ConsoleUI
from inverter_control.control_flags import CONTROL_FLAG_KEYS
from inverter_control.dvcc import create_dvcc_from_config
from inverter_control.evcharger import EvChargerReader
from inverter_control.grid_filter import GridFilter
from inverter_control.homeassistant import get_ha
from inverter_control.logic import SetpointCalculator, SystemState
from inverter_control.metrics import CycleMetrics
from inverter_control.precharge import PrechargeInbox
from inverter_control.prom_metrics import publish as prom_metrics_publish
from inverter_control.submeter_trim import SubmeterTrim
from inverter_control.victron import (
    TOU_END_SETTING,
    TOU_START_SETTING,
    get_victron,
)
from inverter_control.watchdog import HardwareWatchdog
from inverter_control.water import WaterSystemReader
from inverter_control.webhook_server import get_webhook_server
from inverter_control.write_diagnostics import EVENT_LEVELS

logger = logging.getLogger("inverter-control")

# How often the GUI-editable TOU settings are re-read from localsettings
TOU_SETTING_TTL_SECONDS = 60.0

# A control-cycle stage taking longer than this (ms) is flagged as slow in the
# logs. Replaced the old SIGALRM-based cycle abort: every D-Bus call is already
# time-boxed by its own timeout, so this is a symptom diagnostic, not a
# signal-driven force-interrupt (which corrupted cross-thread futures on the
# native-D-Bus reconnect path, 2026-08-27).
STAGE_SLOW_MS = 300.0

# How often (seconds) the full telemetry state-dict is rebuilt for the web UI /
# MQTT. The setpoint control decision runs every cycle in calculate_setpoint and
# reads only the values it truly needs (system data, mppt/pv totals, inverter
# power, grid-smoothing home total, and the setpoint booleans) at full speed.
# Everything else in update_state is telemetry/display-only — acloads, full
# battery & MPPT charger detail, EV/car charge, water level, HA connection
# status, ESS mode, and daily stats — which is NOT used to derive the setpoint,
# so a 3-5 second staleness is acceptable. This cadence decouples that non-critical
# work from the hot path: the heavy reads behind it already refresh on their own
# 2s/5s/10s TTLs in the background poll thread, and the remaining per-build
# composition (dict builds, acload compose) is now ~4-6x rarer than the old 0.5s.
UPDATE_STATE_INTERVAL = 4.0


def log_exception(msg: str):
    """Log exception with full traceback"""
    logger.error(f"{msg}\n{traceback.format_exc()}")


def get_version() -> str:
    """Read version from version file"""
    import os

    try:
        version_file = os.path.join(os.path.dirname(__file__), "..", "version")
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "unknown"


VERSION = get_version()


class InverterController:
    """
    Main controller for grid-zero feed-in management.
    Coordinates I/O (D-Bus, HA) and delegates logic to SetpointCalculator.
    """

    def __init__(self, dry_run: bool | None = None):
        self._start_time = time.time()
        self.dry_run = dry_run if dry_run is not None else DRY_RUN
        self.victron = get_victron()
        self.ha = get_ha(vue_native_get=self.victron.dbus_get_connected)
        self._control_flags = dict.fromkeys(CONTROL_FLAG_KEYS, False)

        # Water comes from dbus-pump D-Bus services (no HA). In test mode the
        # victron client never touches the bus, so skip the reader entirely.
        self.water: BackgroundReader | None = None
        if not getattr(self.victron, "_test_mode", False):
            water = WaterSystemReader(self.victron.dbus_get)
            self.water = BackgroundReader(
                lambda: water.read(force=True),
                {"water_level": None, "water_valve": None, "pump_switch": None},
                name="water-reader",
            )

        # EV charger / vehicle from D-Bus (dbus-evcharger + dbus-ev). Same
        # test-mode guard as water; no HA dependency.
        self.evcharger: BackgroundReader | None = None
        if not getattr(self.victron, "_test_mode", False):
            evcharger = EvChargerReader(self.victron.dbus_get, self.victron.get_service_names)
            self.evcharger = BackgroundReader(
                lambda: evcharger.read(force=True),
                {"ev_power": None, "car_soc": None, "ev_charging_kw": None},
                name="ev-reader",
            )

        # Load UI configuration
        from inverter_control.config import (
            get_ui_config,  # pylint: disable=import-outside-toplevel
        )

        self.ui_config = get_ui_config()
        from .tariff_service import TariffService  # pylint: disable=import-outside-toplevel

        self.tariff = TariffService(self.ui_config.get("electricity_tariff"))
        from .ess_modes import EssSelection

        self.ess_selection = EssSelection()

        # Initialize Logic and UI components
        config_dict = {k: getattr(_config, k) for k in _config.EXPORTED_KEYS}
        self.calculator = SetpointCalculator(config_dict)
        self.submeter_trim = SubmeterTrim(
            enabled=_config.SUBMETER_TRIM_ENABLED is True,
            expected_service=_config.GRID_BACKUP_SERVICE,
        )
        self._trim_decision = None
        self._trim_context = None
        self._trim_mode_generation = 0
        self._trim_history_mode_generation = 0
        self.console = ConsoleUI(self.ha, self.victron, self.water, self.evcharger)

        # Background grid EMA filter (owns filtered_gt; started with the main
        # loop). Not started here so unit tests stay single-threaded.
        self.grid_filter: GridFilter | None = None
        if GRID_FILTER_TAU > 0:
            self.grid_filter = GridFilter(
                getter=self.victron.get_ac_in_power,
                tau=GRID_FILTER_TAU,
            )

        # Second filter for derived_gt (home - pv), same time-based smoothing
        # as the CT filter. Getter reads the attr refreshed each cycle in
        # calculate_setpoint; GridFilter.run skips None ticks before the first
        # cycle lands.
        self.derived_grid_filter: GridFilter | None = None
        if ENABLE_GRID_SMOOTHING_WITH_HOME and GRID_SMOOTHING_DERIVED_TAU > 0:
            self._raw_derived_gt: float | None = None
            self.derived_grid_filter = GridFilter(
                getter=lambda: self._raw_derived_gt,
                tau=GRID_SMOOTHING_DERIVED_TAU,
            )

        # State
        self.current_setpoint = 0
        self.previous_setpoint = 0
        self.manual_setpoint: int | None = None
        self.delay = 0  # Delay counter for load switching
        self.filtered_gt: float | None = None
        self._grid_inputs_invalid = False
        self._control_grid_selection = 0
        self._last_backup_measurement = None

        self.loop_count = 0
        self.state: dict[str, Any] = {}
        self.last_console_line = None
        self._solar_forecast: dict[str, Any] | None = None

        # Pre-charge state (triggered by solar-forecast webhook)
        self._pre_charge_requested = False
        self._precharge = PrechargeInbox(
            None
            if getattr(self.victron, "_test_mode", False)
            else "/data/inverter-control/precharge-requests.json"
        )
        self._pre_charge_horizon_hours = 24
        self._pre_charge_expires_at = 0.0

        # Sustained "ESS not in External control" tracker: GX silently ignores
        # AcPowerSetpoint outside Hub4Mode=3, so live control becomes a no-op.
        self._ess_not_external_since: float | None = None
        self._ess_warn_last_time = 0.0
        self._ess_notification_active = False

        # TOU window settings cache (see _tou_hours)
        self._tou_cache: tuple[int, int] | None = None
        self._tou_cache_time = 0.0
        if not getattr(self.victron, "_test_mode", False):
            self.victron.ensure_tou_settings(
                _config.TOU_EXPENSIVE_START_HOUR, _config.TOU_EXPENSIVE_END_HOUR
            )
            self._load_control_flags()

        # Cached D-Bus data
        self._cached_mppt_data = {}
        self._cached_pv_powers = []
        self._cached_battery_socs = []
        self._cached_inv_state = ""
        self._cached_battery_cell_data = None
        self._cached_batteries = []
        self._cached_mppt_chargers = []
        self._last_cell_data_time = 0.0
        self._last_batteries_time = 0.0
        self._last_chargers_time = 0.0
        self._last_update_state_time = 0.0

        self.telemetry = BackgroundReader(
            self._read_display_telemetry,
            {
                "battery_socs": [],
                "inv_state": "unknown",
                "batteries": [],
                "mppt_chargers": [],
                "loads": {},
                "ess_mode": {},
                "daily_stats": {},
            },
            name="display-telemetry",
            interval=2.0,
            max_age=8.0,
        )

        # Dynamic settings (overridable)
        self.power_limit_min = POWER_LIMIT_MIN
        self.power_limit_max = POWER_LIMIT_MAX
        self.loop_interval = LOOP_INTERVAL
        # Rolling latency metrics for hardware-run benchmarking (see metrics.py)
        self.metrics = CycleMetrics()
        self._last_perf_snapshot = None
        self.performance = BackgroundReader(
            self._read_performance,
            {},
            name="performance-telemetry",
            interval=5.0,
            max_age=15.0,
        )

        # DVCC Calculator for dynamic battery current limits (SoC & Cell Temp curves)
        if DVCC_ENABLED:
            cell_counts = self.victron.get_cell_counts()
            # CVL is a system-level voltage limit: battery chains are parallel,
            # so use the per-chain cell count, not the sum across chains.
            cells_per_chain = max(cell_counts.values()) if cell_counts else 16
            self.dvcc_calculator = create_dvcc_from_config(
                {
                    "DVCC_CELL_COUNT": cells_per_chain,
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
            self.dvcc_limits: dict[str, Any] | None = None
        else:
            self.dvcc_calculator = None
            self.dvcc_limits = None

        # Hardware watchdog - triggers fallback if telemetry stops.
        # Started explicitly in _run_main_loop (not here) to avoid triggering
        # during a slow startup sequence.
        self._watchdog = HardwareWatchdog(
            victron=self.victron,
            timeout_seconds=WATCHDOG_TIMEOUT_SECONDS,
            check_interval=WATCHDOG_CHECK_INTERVAL,
            dry_run=self.dry_run,
            get_setpoint=lambda: self.previous_setpoint,
            grid_loss_hold_seconds=_config.GRID_LOSS_HOLD_SECONDS,
            diagnostics=self.victron.write_diagnostics,
        )
        self._control_history_generation = self._watchdog.control_generation()

        # Webhook server for pre-charge triggers from solar-forecast
        self._webhook_server = get_webhook_server(
            host=WEBHOOK_SERVER_HOST,
            port=WEBHOOK_SERVER_PORT,
            pre_charge_callback=self._handle_pre_charge_webhook,
            forecast_callback=self._handle_forecast_webhook,
        )
        self._webhook_server.start()

    def set_loop_interval(self, interval: float) -> float:
        self.loop_interval = max(0.1, min(5.0, interval))
        logger.info(f"Loop interval changed to {self.loop_interval}s")
        return self.loop_interval

    def set_power_limits(self, min_val: int, max_val: int) -> dict[str, int]:
        if type(min_val) is not int or type(max_val) is not int or min_val > max_val:
            raise ValueError("Power limits must be ordered integers")
        with self._watchdog._lock:
            self.power_limit_min = max(-3000, min(3000, min_val))
            self.power_limit_max = max(-3000, min(3000, max_val))
            # Update calculator limits
            self.calculator.power_limit_min = self.power_limit_min
            self.calculator.power_limit_max = self.power_limit_max
            self._trim_mode_generation += 1
        logger.info(f"Power limits changed to [{self.power_limit_min}, {self.power_limit_max}]")
        return {"min": self.power_limit_min, "max": self.power_limit_max}

    def set_dry_run(self, enabled: bool) -> bool:
        with self._watchdog._lock:
            if self.dry_run != enabled:
                self._trim_mode_generation += 1
            self.dry_run = enabled
            self._watchdog.dry_run = enabled
            # toggle_dry_run also owns this lock; sinks must run off the write path.
            self._watchdog._record_diagnostic("dry_run_changed", dry_run=enabled)
            return self.dry_run

    def toggle_dry_run(self) -> bool:
        with self._watchdog._lock:
            return self.set_dry_run(not self.dry_run)

    def select_ess_mode(self, payload: dict) -> None:
        def write(mode):
            with self._watchdog._lock:
                if self.dry_run:
                    raise RuntimeError("ESS changes are unavailable in DRY mode")
                self.victron.select_ess_mode(mode)
                self._trim_mode_generation += 1

        self.ess_selection.apply(payload, write)

    def toggle_ess_mode(self) -> dict[str, Any]:
        current = self.victron.get_ess_mode()
        new_external = not current["is_external"]
        if self.victron.set_ess_mode(external=new_external):
            new_mode = self.victron.get_ess_mode()
            logger.info(f"ESS Mode changed to {new_mode['mode_name']}")
            return new_mode
        return current

    def _in_expensive_window(self) -> bool:
        """True while inside the TOU expensive window (if configured).

        Hours come from the GUI-editable localsettings entries
        (/Settings/InverterControl/TouExpensive*) so they can be changed
        from the Venus Settings menu without SSH. Falls back to the
        local_config/env values when the settings are unreadable.
        Hour comes from the GX timezone setting (/Settings/System/TimeZone),
        so the window follows the user's wall clock even though the Venus
        system clock runs UTC.
        """
        start, end = self._tou_hours()
        if start < 0 or end < 0 or start == end:
            return False
        hour = self.victron.get_local_hour()
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # wraps midnight

    def _tou_hours(self) -> tuple[int, int]:
        """TOU window hours from localsettings, refreshed at most once per
        TOU_SETTING_TTL_SECONDS (subprocess D-Bus reads are not free on RPi)."""
        now = time.time()
        if self._tou_cache is None or now - self._tou_cache_time >= TOU_SETTING_TTL_SECONDS:
            start = self.victron.get_tou_setting(TOU_START_SETTING)
            end = self.victron.get_tou_setting(TOU_END_SETTING)
            self._tou_cache = (
                _config.TOU_EXPENSIVE_START_HOUR if start is None else start,
                _config.TOU_EXPENSIVE_END_HOUR if end is None else end,
            )
            self._tou_cache_time = now
        return self._tou_cache

    def _handle_pre_charge_webhook(self, payload: dict) -> dict:
        """Persist one decision before scheduling the existing one-cycle intent."""

        def accept():
            with self._watchdog._lock:
                self._pre_charge_expires_at = payload["expires_at"]
                self._pre_charge_requested = True
                self._pre_charge_horizon_hours = payload.get("horizon_hours", 24)
                self._trim_mode_generation += 1

        outcome = self._precharge.handle(payload, self._in_expensive_window, accept)
        if outcome["status"] in {"accepted", "suppressed"}:
            from inverter_control.mqtt_bridge import get_mqtt_bridge

            bridge = get_mqtt_bridge()
            if bridge:
                bridge.publish_notification(
                    notification_id="precharge-" + outcome["request_id"],
                    level="info",
                    title="Pre-charge " + outcome["status"],
                    body=outcome["reason"],
                    source="solar-forecast",
                )
        return outcome

    def _handle_forecast_webhook(self, payload: dict) -> bool:
        """Store daily forecast summary from solar-forecast-langgraph.

        Included in the published state so dashboards can display the
        solar outlook next to actual production figures.
        """
        try:
            self._solar_forecast = {
                k: payload[k]
                for k in ("date", "today_kwh", "tomorrow_kwh", "generated_at", "site_id")
                if k in payload
            }
            logger.info(
                f"Forecast stored: today={self._solar_forecast.get('today_kwh')}kWh "
                f"tomorrow={self._solar_forecast.get('tomorrow_kwh')}kWh"
            )
            return True
        except Exception:
            logger.exception("Error handling forecast webhook")
            return False

    def get_state(self) -> dict[str, Any]:
        return {**self.state, "ui_config": {**self.ui_config, **self.tariff.snapshot()}}

    def set_manual_setpoint(self, value: int) -> bool:
        with self._watchdog._lock:
            self.manual_setpoint = max(self.power_limit_min, min(self.power_limit_max, value))
            self._trim_mode_generation += 1
        return True

    def get_setpoint_override(self) -> dict:
        return self._watchdog.get_setpoint_override()

    def set_setpoint_override(self, value: int | None, request_id: str | None = None) -> dict:
        status = self._watchdog.set_setpoint_override(value, request_id)
        if status["last_error"] is None:
            # Reset calculation history on the main thread's next cycle; MQTT
            # callbacks must not mutate a calculator currently in use.
            self.manual_setpoint = None
        self._update_grid_loss_state()
        return status

    def start_auxiliary_readers(self) -> None:
        """Start bounded-age auxiliary snapshots without blocking startup."""
        for reader in (self.water, self.evcharger, self.telemetry, self.performance):
            if reader is not None:
                reader.start()

    def request_stop_auxiliary_readers(self) -> None:
        for reader in (self.water, self.evcharger, self.telemetry, self.performance):
            if reader is not None:
                reader.request_stop()

    def stop_auxiliary_readers(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        self.request_stop_auxiliary_readers()
        stopped = True
        for reader in (self.water, self.evcharger, self.telemetry, self.performance):
            if reader is not None:
                stopped = reader.stop(max(0.0, deadline - time.monotonic())) and stopped
        return stopped

    def calculate_setpoint(self, sys_data: dict[str, Any]) -> tuple[int, str]:
        """Orchestrate state collection and delegate calculation to logic.py"""
        self._trim_decision = None
        self._trim_context = None
        if (
            sys_data.get("_grid_backup")
            and sys_data.get("_grid_measurement_time") == self._last_backup_measurement
        ):
            self._reset_submeter_trim("backup_source")
            # HA can report more slowly than the control loop. Refresh the
            # accepted output, but apply feedback only once per source sample.
            return self.previous_setpoint, "[SUBMETER HOLD] "
        # Prepare SystemState snapshot
        mppt_data = self.victron.get_mppt_data()
        mppt_total = sum(m["w"] for m in mppt_data.values())
        pv_inverter_powers = self.victron.get_pv_power()
        pv_inverter_total = sum(pv_inverter_powers)

        # Cache these reads so update_state doesn't re-query D-Bus this cycle
        self._cached_mppt_data = mppt_data
        self._cached_pv_powers = pv_inverter_powers

        # Grid smoothing with Home total (Vue via D-Bus)
        # derived_gt = home_total - pv_total (negative = export, positive = import)
        # Blend with instantaneous CT meter for stable control
        home_total = 0.0
        derived_gt = None
        if ENABLE_GRID_SMOOTHING_WITH_HOME and not sys_data.get("_grid_backup"):
            home_total = self.ha.get_vue_sensor("total", 0)
            if home_total > 0:
                pv_total = mppt_total + pv_inverter_total
                derived_gt = home_total - pv_total
                # Feed the raw value to the background filter; the state gets
                # one coherent smoothed snapshot (None until first tick).
                if self.derived_grid_filter is not None:
                    self._raw_derived_gt = derived_gt
                    derived_gt = self.derived_grid_filter.value()

        # Handle pre-charge request from solar forecast webhook
        charge_battery = self.get_control_flag("charge_battery")
        with self._watchdog._lock:
            pre_charge_requested = self._pre_charge_requested
            self._pre_charge_requested = False  # One-shot
        if pre_charge_requested:
            if time.time() >= self._pre_charge_expires_at:
                logger.info("Pre-charge suppressed: queued request expired")
            elif self._in_expensive_window():
                logger.info("Pre-charge suppressed: expensive grid window active")
            else:
                charge_battery = True
                logger.info("Pre-charge triggered by solar forecast")

        # HA-heavy SystemState snapshot
        state = SystemState(
            g1=sys_data["g1"],
            g2=sys_data["g2"],
            gt=sys_data["gt"],
            t1=sys_data["t1"],
            t2=sys_data["t2"],
            tt=sys_data["tt"],
            inv_power=self.victron.get_inverter_power(),
            mppt_total=mppt_total,
            pv_inverter_total=pv_inverter_total,
            pv_total=mppt_total + pv_inverter_total,
            ev_power=(self.evcharger.read()["ev_power"] or 0) if self.evcharger else 0,
            garage_power=self.ha.get_vue_sensor("garage", 0),
            home_total=home_total,
            only_charging=self.get_control_flag("only_charging"),
            no_feed=self.get_control_flag("no_feed"),
            house_support=self.get_control_flag("house_support"),
            charge_battery=charge_battery,
            do_not_supply_charger=self.get_control_flag("do_not_supply_charger"),
            limit_to_ev=self.get_control_flag("set_limit_to_ev_charger"),
            previous_setpoint=self.previous_setpoint,
            filtered_gt=self.filtered_gt,
            prefiltered_gt=(self.grid_filter.value() if self.grid_filter else None),
            derived_gt=derived_gt,
        )

        # Perform calculation
        result = self.calculator.calculate(state)

        # Mirror for display/MQTT state; the authoritative smoothed value
        # lives in the GridFilter thread when it is running.
        self.filtered_gt = result.filtered_gt

        if not self.submeter_trim.enabled:
            return result.setpoint, result.flags
        if self._trim_history_mode_generation != self._trim_mode_generation:
            self._reset_submeter_trim("mode_changed")
            self._trim_history_mode_generation = self._trim_mode_generation
        trim_mode_generation = self._trim_mode_generation
        reason = self._trim_block_reason(sys_data, charge_battery=charge_battery)
        if not reason and (
            result.setpoint != self.previous_setpoint
            or not _config.GRID_ZERO_DEADBAND_LOW
            < result.filtered_gt
            < _config.GRID_ZERO_DEADBAND_HIGH
            or "[B:" in result.flags
            or "[D:" in result.flags
        ):
            reason = "fast_control_active"
        # Apply only after the ordinary calculator has decided to hold. This
        # leaves D suppression intact and avoids the 0.9 integer convergence
        # swallowing a small accepted-command correction.
        decision = self.submeter_trim.propose(
            now=time.monotonic(),
            wall_time=time.time(),
            previous_setpoint=self.previous_setpoint,
            base_setpoint=result.setpoint,
            sample=sys_data.get("_grid_backup_status"),
            eligible=not reason,
            reason=reason,
            raw_grid=sys_data["gt"],
            filtered_grid=result.filtered_gt,
            min_setpoint=self.power_limit_min,
            max_setpoint=self.power_limit_max,
            source_key=self._trim_source_key(sys_data),
        )
        self._trim_decision = decision
        self._trim_context = self._submeter_trim_context(sys_data)[:2] + (trim_mode_generation,)
        if (
            abs(decision.setpoint - self.previous_setpoint) > self.calculator.delta_limit
            and decision.delta
        ):
            self._reset_submeter_trim("delta_limit")
            return result.setpoint, result.flags
        if decision.delta and not self._trim_stays_in_primary_hold(decision.delta):
            self._reset_submeter_trim("primary_deadband_limit")
            return result.setpoint, result.flags
        flags = result.flags
        if decision.delta:
            flags += f"[TRIM:{decision.delta:+d}] "
        return decision.setpoint, flags

    @staticmethod
    def _trim_source_key(sys_data: dict[str, Any]) -> tuple:
        return (
            sys_data.get("_grid_source"),
            sys_data.get("_grid_source_instance"),
            sys_data.get("_grid_selection_generation"),
        )

    def _submeter_trim_context(self, sys_data: dict[str, Any]) -> tuple:
        sample = sys_data.get("_grid_backup_status") or {}
        return (
            self._trim_source_key(sys_data),
            tuple(
                sample.get(key)
                for key in ("service", "device_instance", "generation", "measurement_time")
            ),
            self._trim_mode_generation,
        )

    def _trim_block_reason(self, sys_data: dict[str, Any], *, charge_battery=False) -> str:
        if (
            sys_data.get("_grid_valid") is not True
            or sys_data.get("_grid_primary_valid") is not True
            or sys_data.get("_grid_backup") is not False
        ):
            return "primary_unavailable"
        if not _config.GRID_BACKUP_SERVICE:
            return "submeter_not_pinned"
        if (
            charge_battery
            or self._pre_charge_requested
            or any(self.get_control_flag(key) for key in CONTROL_FLAG_KEYS)
        ):
            return "operating_mode"
        if self.manual_setpoint is not None or self.get_setpoint_override()["value"] is not None:
            return "manual_control"
        if self._watchdog.is_triggered():
            return "watchdog"
        # The optional smoothing flag alone does not activate derived feedback:
        # an installation may have no configured/discovered Vue total sensor.
        # Presence is deliberately conservative, including zero/unknown values,
        # and this same gate is checked again immediately before a trim write.
        if _config.CREEP_RATE != 0 or (
            ENABLE_GRID_SMOOTHING_WITH_HOME and "total" in self.ha.get_all_vue_sensors()
        ):
            return "incompatible_feedback"
        if self.victron.get_ess_mode().get("is_external") is not True:
            return "ess_not_external"
        return ""

    def _reset_submeter_trim(self, reason: str) -> None:
        self.submeter_trim.reset(reason)
        self._trim_decision = None
        self._trim_context = None

    def _trim_stays_in_primary_hold(self, delta: int) -> bool:
        # Avoid deliberately crossing the fast controller's hold boundary and
        # creating a slow correction/fast undo cycle between the two meters.
        return self.filtered_gt is not None and (
            _config.GRID_ZERO_DEADBAND_LOW
            < self.filtered_gt + delta
            < _config.GRID_ZERO_DEADBAND_HIGH
        )

    def _trim_write_ready(self, current_grid: dict[str, Any]) -> bool:
        decision = self._trim_decision
        if decision is None or not decision.delta:
            return True
        if self._trim_context != self._submeter_trim_context(current_grid):
            return False
        if self._trim_block_reason(current_grid):
            return False
        if abs(decision.setpoint - self.previous_setpoint) > self.calculator.delta_limit:
            return False
        if not self._trim_stays_in_primary_hold(decision.delta):
            return False
        sample = current_grid.get("_grid_backup_status") or {}
        # Revalidate the proposal against fresh availability/age and the raw
        # grid just before writing. A sample/source edge requires a new cycle.
        refreshed = self.submeter_trim.propose(
            now=time.monotonic(),
            wall_time=time.time(),
            previous_setpoint=self.previous_setpoint,
            base_setpoint=self.previous_setpoint,
            sample=sample,
            eligible=True,
            reason="",
            raw_grid=current_grid.get("gt"),
            filtered_grid=self.filtered_gt,
            min_setpoint=self.power_limit_min,
            max_setpoint=self.power_limit_max,
            source_key=self._trim_source_key(current_grid),
        )
        return refreshed == decision

    def handle_minimize_charging(self, sys_data: dict[str, Any]):
        try:
            if self.delay > 0:
                self.delay -= 1
                return
            if not self.get_control_flag("minimize_charging"):
                return
            inverter_state, _ = self.victron.get_inverter_state()
            if inverter_state == 0:
                return
            net_usage = self.ha.get_sensor("net_usage", 0)
            bp = sys_data.get("bp")
            if bp is None:
                return
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

    def _get_cached_batteries(self) -> list:
        now = time.time()
        if now - self._last_batteries_time > 10:
            self._cached_batteries = self.victron.get_all_batteries()
            self._last_batteries_time = now
        return self._cached_batteries

    def _get_cached_mppt_chargers(self) -> list:
        now = time.time()
        if now - self._last_chargers_time > 10:
            self._cached_mppt_chargers = self.victron.get_mppt_chargers()
            self._last_chargers_time = now
        return self._cached_mppt_chargers

    def _get_ev_state(self) -> dict[str, Any]:
        if not ENABLE_EV:
            return {"ev_power": 0, "car_soc": 0, "ev_charging_kw": 0}
        if self.evcharger is None:
            # Test mode / no D-Bus: report no data rather than fake zeros
            return {"ev_power": 0, "car_soc": 0, "ev_charging_kw": 0}
        data = self.evcharger.read()
        return {
            "ev_power": data.get("ev_power") or 0,
            "car_soc": data.get("car_soc") or 0,
            "ev_charging_kw": data.get("ev_charging_kw") or 0,
        }

    def _get_water_state(self) -> dict[str, Any]:
        if not ENABLE_WATER:
            return {"water_level": None, "water_valve": None, "pump_switch": None}
        if self.water is None:
            # Test mode / no D-Bus: report no data rather than fake zeros
            return {"water_level": None, "water_valve": None, "pump_switch": None}
        return self.water.read()

    def _get_ha_status(self) -> dict[str, Any]:
        return {
            "ha_connected": self.ha.connected if ENABLE_HA else False,
            "ha_uptime": self.ha.uptime if ENABLE_HA else 0,
        }

    def get_control_flag(self, key: str) -> bool:
        """In-process control flags. Never reads Home Assistant for these keys."""
        return bool(self._control_flags.get(key, False))

    def _load_control_flags(self) -> None:
        """Cold-start all flags False. Register Settings (default 0). No HA/Settings restore."""
        self._control_flags = dict.fromkeys(CONTROL_FLAG_KEYS, False)
        try:
            self.victron.ensure_control_flag_settings(dict.fromkeys(CONTROL_FLAG_KEYS, 0))
        except Exception:
            logger.exception("Failed to register control flag Settings")
        # Keep Venus UI / N/settings in sync with cold-start all-off.
        for key in CONTROL_FLAG_KEYS:
            try:
                if not self.victron.set_control_flag(key, 0):
                    logger.warning("Settings reset to 0 failed for %s", key)
            except Exception:
                logger.exception("Failed to reset control flag %s", key)

    def set_control_flag(self, key: str, value: bool) -> None:
        if key not in self._control_flags:
            logger.warning("Unknown control flag %s", key)
            return
        value = bool(value)
        # Serialize flag changes with the final trim gate and physical write.
        with self._watchdog._lock:
            if self._control_flags[key] != value:
                self._trim_mode_generation += 1
            self._control_flags[key] = value
        try:
            if getattr(self.victron, "_test_mode", False):
                pass  # ponytail: skip Settings write in test mode
            elif not self.victron.set_control_flag(key, int(value)):
                logger.warning("Settings write failed for %s=%s", key, int(value))
        except Exception:
            logger.exception("Failed to persist control flag %s", key)
        try:
            from inverter_control.mqtt_bridge import get_mqtt_bridge

            bridge = get_mqtt_bridge()
            if bridge:
                bridge.publish_state(self.get_state_for_mqtt())
        except Exception:
            logger.exception("Failed to publish inverter/state after set_control_flag")

    def get_boolean(self, key: str) -> bool:
        """Compatibility alias for callers of the original control API."""
        return self.get_control_flag(key)

    def set_boolean(self, key: str, value: bool) -> None:
        """Compatibility alias; the flag is owned by this daemon, never HA."""
        self.set_control_flag(key, value)

    def _get_daily_stats(self) -> dict[str, Any]:
        # All daily stats now from D-Bus (no HA dependency)
        battery_in, battery_out = self.victron.get_battery_daily_energy()
        battery_in_yesterday, battery_out_yesterday = self.victron.get_battery_yesterday_energy()
        mppt_daily = self.victron.get_mppt_daily_yields()
        pv_inverter_daily = self.victron.get_pv_inverter_daily_yields()
        produced_today = sum(mppt_daily) + sum(pv_inverter_daily)
        mppt_yesterday = self.victron.get_mppt_yesterday_yields()
        pv_inverter_yesterday = self.victron.get_pv_inverter_yesterday_yields()
        produced_yesterday = sum(mppt_yesterday) + sum(pv_inverter_yesterday)

        return {
            "produced_today": produced_today,
            "produced_yesterday": produced_yesterday,
            "grid_kwh": None,  # Legacy field: directional energy has its own coverage contract
            "grid_energy": self.victron.get_grid_daily_energy(),
            "battery_in": battery_in,
            "battery_out": battery_out,
            "battery_in_yesterday": battery_in_yesterday,
            "battery_out_yesterday": battery_out_yesterday,
            "pv_total_daily": produced_today,
            "pv_inverter_daily": pv_inverter_daily,
            "mppt_daily": mppt_daily,
            "pv_inverter_yesterday": pv_inverter_yesterday,
            "mppt_yesterday": mppt_yesterday,
        }

    def _read_display_telemetry(self) -> dict:
        """All display-only I/O runs on the snapshot worker, never the control loop."""
        self.victron.persist_grid_energy()
        return {
            "battery_socs": self.victron.get_battery_chain_socs(),
            "inv_state": self.victron.get_inverter_state()[1],
            "batteries": self._get_cached_batteries(),
            "mppt_chargers": self._get_cached_mppt_chargers(),
            "loads": self.victron.get_acload_powers() if ENABLE_ACLOADS else {},
            "ess_mode": self.victron.get_ess_mode(),
            "daily_stats": self._get_daily_stats(),
        }

    def _read_performance(self) -> dict:
        """Procfs, percentile sorting and exporter locks stay off the control loop."""
        for diagnostic in self.victron.drain_write_diagnostics():
            if diagnostic["event"] == "control_stage_slow":
                logger.warning(
                    "Control cycle stage %s slow: %.0fms",
                    diagnostic["stage"],
                    diagnostic["seconds"] * 1000.0,
                )
            else:
                logger.log(
                    EVENT_LEVELS[diagnostic["event"]], "Hardware write diagnostic: %s", diagnostic
                )
        for timing in self.victron.drain_write_timings():
            logger.warning("Native D-Bus write timing: %s", timing)
        self.metrics.sample_process()
        perf = self.metrics.snapshot()
        perf["signals_healthy"] = bool(self.victron.is_signals_healthy())
        perf["dbus_subprocess_calls"] = int(getattr(self.victron, "subprocess_calls", 0))
        perf["sampled_at_unix"] = time.time()
        prom_metrics_publish(perf)
        return {"perf": perf}

    def update_state(self, sys_data: dict[str, Any], setpoint: int):
        display = self.telemetry.read()
        self._cached_battery_socs = display["battery_socs"]
        self._cached_inv_state = display["inv_state"]
        sys_data["mppt_data"] = self._cached_mppt_data
        sys_data["pv_inverter_powers"] = self._cached_pv_powers
        sys_data["battery_socs"] = self._cached_battery_socs
        batteries = display["batteries"]
        mppt_chargers = display["mppt_chargers"]
        ev_state = self._get_ev_state()
        water_state = self._get_water_state()
        ha_status = self._get_ha_status()
        loads = display["loads"]
        ess_mode = display["ess_mode"]
        daily_stats = display["daily_stats"]

        mppt_total = sum(m["w"] for m in self._cached_mppt_data.values())
        pv_total = sum(self._cached_pv_powers)

        # Full state for web UI
        self.state = {
            "grid_control_valid": sys_data.get("_grid_valid") is True,
            "grid_control_reason": sys_data.get("_grid_invalid_reason"),
            **sys_data,
            **self._grid_status_fields(sys_data),
            "setpoint": setpoint,
            "filtered_gt": self.filtered_gt,
            "dry_run": self.dry_run,
            "mppt_total": mppt_total,
            "pv_inverter_total": pv_total,
            "solar_total": mppt_total + pv_total,
            "mppt_data": self._cached_mppt_data,
            "mppt_individual": [m["w"] for m in self._cached_mppt_data.values()],
            "pv_inverter_individual": self._cached_pv_powers,
            "inverter_state": self._cached_inv_state,
            "battery_socs": self._cached_battery_socs,
            "batteries": batteries,
            "mppt_chargers": mppt_chargers,
            **ev_state,
            **water_state,
            **ha_status,
            "booleans": self._control_flags,
            "loads": loads,
            "ess_mode": ess_mode,
            "battery_power": sys_data.get("bp"),
            "battery_voltage": sys_data.get("bv"),
            "battery_current": sys_data.get("bc"),
            "battery_soc": self.victron.get_battery_soc_local(sys_data),
            "daily_stats": daily_stats,
            "solar_forecast": self._solar_forecast,
            "limits": {"min": self.power_limit_min, "max": self.power_limit_max},
            "loop_interval": self.loop_interval,
            "version": VERSION,
            "uptime": int(time.time() - self._start_time),
            "ui_config": {**self.ui_config, **self.tariff.snapshot()},
            "dvcc_limits": self.dvcc_limits if self.dvcc_limits else None,
        }
        self._update_grid_loss_state()
        performance = self.performance.read()
        sampled_at = performance.get("perf", {}).get("sampled_at_unix")
        if sampled_at is not None and sampled_at != self._last_perf_snapshot:
            # Refresh diagnostics in UI/MQTT state only for a newly sampled result.
            self.state.update(performance)
            self._last_perf_snapshot = sampled_at
        self._check_ess_external()

    def _check_ess_external(self) -> None:
        """Warn loudly when live control is being silently ignored.

        GX only honors /Hub4/L1/AcPowerSetpoint while ESS is in External
        control (Hub4Mode=3); a manual switch to Optimized (BatteryLife)
        turns every setpoint write into a no-op with no other feedback -
        the incident's grid-zero failure mode.
        """
        from inverter_control.mqtt_bridge import (  # pylint: disable=import-outside-toplevel
            get_mqtt_bridge,
        )

        if self.dry_run:
            # Writes are intentionally disabled; the mismatch is expected,
            # so don't accumulate the timer or hold the banner.
            self._clear_ess_warning()
            return

        ess = self.state.get("ess_mode") or {}
        now = time.time()

        if "is_external" not in ess:
            self._clear_ess_warning()
            return

        if ess.get("is_external"):
            if self._ess_notification_active:
                bridge = get_mqtt_bridge()
                if bridge:
                    bridge.publish_notification(
                        notification_id="ess-not-external",
                        level="info",
                        title="ESS External control restored",
                        body="Grid setpoint writes are honored again.",
                    )
                logger.info(
                    "ESS back in External control after %.0f min",
                    (now - (self._ess_not_external_since or now)) / 60.0,
                )
            self._clear_ess_warning()
            return

        if self._ess_not_external_since is None:
            self._ess_not_external_since = now
            return
        since = self._ess_not_external_since
        if now - since < ESS_EXTERNAL_WARN_MINUTES * 60.0:
            return

        if not self._ess_notification_active:
            bridge = get_mqtt_bridge()
            if bridge:
                bridge.publish_notification(
                    notification_id="ess-not-external",
                    level="warning",
                    title="ESS not in External control",
                    body=(
                        "Grid setpoint writes are being IGNORED by the GX. "
                        "Switch the ESS assistant to 'External control' "
                        "(Hub4Mode=3) on the GX to restore grid-zero control."
                    ),
                )
            logger.warning(
                "ESS not in External control for %.0f min - setpoint writes are no-ops",
                (now - since) / 60.0,
            )
            self._ess_notification_active = True
            self._ess_warn_last_time = now
        elif now - self._ess_warn_last_time >= 3600.0:
            logger.warning(
                "ESS still not in External control (%.0f min) - control remains a no-op",
                (now - since) / 60.0,
            )
            self._ess_warn_last_time = now

    def _clear_ess_warning(self) -> None:
        """Reset the sustained-mismatch tracker (recovery, dry-run, startup)."""
        self._ess_notification_active = False
        self._ess_not_external_since = None

    def get_state_for_mqtt(self) -> dict[str, Any]:
        """Publish payload for inverter/state.

        When MQTT_SLIM_STATE is enabled, drop Cerbo/dbus-mirrored live tiles
        (grid, consumption, bank, solar, loads, EV, water, setpoint/mode) so
        consumers that already read Victron MQTT do not get a duplicate mirror.
        """
        out = dict(self.state)
        if MQTT_SLIM_STATE:
            for k in MQTT_SLIM_EXCLUDE_KEYS:
                out.pop(k, None)
        # Always publish current flags so MQTT/HA see set_control_flag immediately
        out["booleans"] = dict(self._control_flags)
        out["dry_run"] = self.dry_run
        out["ess_mode"] = self.ess_selection.observe(self.victron.get_ess_mode)
        # Daemon-owned control intent is never stripped by the slim payload.
        out["setpoint_override"] = self.get_setpoint_override()
        out["submeter_trim"] = self.submeter_trim.status()
        # Include presentation even before the first telemetry sweep.
        out["ui_config"] = {**self.ui_config, **self.tariff.snapshot()}
        return out

    def _update_dvcc_limits(self) -> None:
        """Update DVCC limits if calculator is available and cache is stale."""
        if self.dvcc_calculator is None:
            return

        now = time.time()
        if now - self._last_cell_data_time > 30:
            battery_data = self.victron.get_battery_cell_data()
            self._cached_battery_cell_data = battery_data
            self._last_cell_data_time = now

        if self._cached_battery_cell_data is not None:
            self.dvcc_limits = self.dvcc_calculator.calculate(self._cached_battery_cell_data)

    @staticmethod
    def _grid_status_fields(sys_data: dict[str, Any]) -> dict[str, Any]:
        """Map the current grid snapshot to public telemetry fields."""
        return {
            "grid_control_source": sys_data.get("_grid_source"),
            "grid_control_power": sys_data.get("gt") if sys_data.get("_grid_valid") else None,
            "grid_using_backup": sys_data.get("_grid_backup", False),
            "grid_backup_available": sys_data.get("_grid_backup_available", False),
            "grid_backup": sys_data.get("_grid_backup_status"),
            "grid_primary_reason": sys_data.get("_grid_primary_reason"),
            "grid_source_transitions": [
                dict(event) for event in sys_data.get("_grid_source_transitions", [])
            ],
        }

    def _grid_ready_for_control(self, sys_data: dict[str, Any]) -> bool:
        """Gate control on a usable grid snapshot and orderly watchdog recovery."""
        self.state.update(self._grid_status_fields(sys_data))
        if sys_data.get("_grid_valid") is not True:
            self._reset_submeter_trim("primary_invalid")
            self._watchdog.mark_dbus_invalid()
            # The same watchdog owns both outage and stalled-loop writes.
            # Check at control cadence so a short hold is not rounded to its
            # slower background check interval. Never recalculate stale data.
            self._watchdog.check_grid_loss()
            if not self._grid_inputs_invalid:
                logger.warning(
                    "Grid telemetry unavailable; control paused: %s",
                    sys_data.get("_grid_invalid_reason", "validity not provided"),
                )
                self.filtered_gt = None
                self._raw_derived_gt = None
                self.calculator.reset_measurement_history()
                for grid_filter in (self.grid_filter, self.derived_grid_filter):
                    if grid_filter is not None:
                        grid_filter.reset()
            self._grid_inputs_invalid = True
            self.state["grid_control_valid"] = False
            self.state["grid_control_reason"] = sys_data.get("_grid_invalid_reason")
            self._update_grid_loss_state()
            return False
        self._watchdog.mark_dbus_update()
        selection = sys_data.get("_grid_selection_generation", 0)
        if selection != self._control_grid_selection:
            self._reset_submeter_trim("source_changed")
            self._control_grid_selection = selection
            self._last_backup_measurement = None
            self.filtered_gt = None
            self._raw_derived_gt = None
            self.calculator.reset_measurement_history()
            for grid_filter in (self.grid_filter, self.derived_grid_filter):
                if grid_filter is not None:
                    grid_filter.reset()
            logger.info("Grid control source changed to %s", sys_data.get("_grid_source"))
            return False  # Recalculate on the next cycle, including before-write switches.
        if self._watchdog.is_triggered():
            # Its two-check recovery must finish (including an accepted restore)
            self._reset_submeter_trim("watchdog")
            # before a fresh normal write can otherwise race with that restore.
            self.state["grid_control_valid"] = False
            self.state["grid_control_reason"] = "Waiting for watchdog recovery"
            self._watchdog.check_grid_loss()
            self._update_grid_loss_state()
            return False
        if self._grid_inputs_invalid:
            logger.info("Grid telemetry recovered; control resumed")
        self._grid_inputs_invalid = False
        self.state["grid_control_valid"] = True
        self.state["grid_control_reason"] = None
        self._update_grid_loss_state()
        return True

    def _update_grid_loss_state(self) -> None:
        """Expose outage progress even when regular telemetry rebuilds pause."""
        # Keep the applied baseline synchronized with Start/Stop and normal
        # writes; no snapshot from before a mode change may overwrite it later.
        with self._watchdog._lock:
            self._update_grid_loss_state_locked()

    def _update_grid_loss_state_locked(self) -> None:
        status = self._watchdog.get_status()
        self.state.update(
            {key: value for key, value in status.items() if key.startswith("grid_loss_")}
        )
        override = self.get_setpoint_override()
        self.state["setpoint_override"] = override
        if override["value"] is not None:
            # A failed refresh keeps the last accepted manual value and its
            # explicit error; automatic policies cannot substitute another one.
            self.previous_setpoint = override["value"]
            self.current_setpoint = override["value"]
            self.state["setpoint"] = override["value"]
            return
        if status["grid_loss_state"] == "holding":
            self.state["setpoint"] = self.previous_setpoint
        if status["grid_loss_fallback_applied"]:
            # Only an accepted safety write changes the displayed/applied state.
            fallback = status["grid_loss_fallback_setpoint"]
            self.previous_setpoint = fallback
            self.current_setpoint = fallback
            self.state["setpoint"] = fallback

    def run_cycle(self) -> bool:
        cycle_started = time.monotonic()
        stage_started = time.perf_counter()

        def _stage(name: str) -> None:
            """Record duration of the stage that just ended."""
            nonlocal stage_started
            now = time.perf_counter()
            elapsed_ms = (now - stage_started) * 1000.0
            self.metrics.record_stage(name, elapsed_ms)
            # Surface an unexpectedly slow stage via metrics/logging. We do NOT
            # abort the cycle on a signal: each native/CLI D-Bus call is already
            # time-boxed by its own timeout, so a slow stage is a symptom to
            # debug, not a hang to force-interrupt (the old SIGALRM approach
            # corrupted cross-thread futures on the reconnect path, 2026-08-27).
            if elapsed_ms > STAGE_SLOW_MS:
                self._watchdog._record_diagnostic(
                    "control_stage_slow", stage=name, seconds=elapsed_ms / 1000.0
                )
            # Keep diagnostic overhead out of the next stage, but in the full cycle.
            stage_started = time.perf_counter()

        try:
            self.last_console_line = None
            self._trim_decision = None
            self._trim_context = None
            generation = self._watchdog.control_generation()
            if generation != self._control_history_generation:
                self._reset_submeter_trim("control_generation")
                self.filtered_gt = None
                self._raw_derived_gt = None
                self.calculator.reset_measurement_history()
                for grid_filter in (self.grid_filter, self.derived_grid_filter):
                    if grid_filter is not None:
                        grid_filter.reset()
                self._control_history_generation = generation
            sys_data = self.victron.get_system_data()
            if self.get_setpoint_override()["value"] is not None:
                self._reset_submeter_trim("manual_control")
                # Observe meter health for the later Stop transition, but the
                # watchdog independently maintains the explicit manual value.
                self._grid_ready_for_control(sys_data)
                self._watchdog.check_grid_loss()
                self._update_grid_loss_state()
                self.state["grid_control_valid"] = False
                self.state["grid_control_reason"] = "Manual setpoint override active"
                self.metrics.record_cycle(cycle_started, self.loop_interval)
                return True
            if not self._grid_ready_for_control(sys_data):
                # Preserve pending manual/precharge requests and all control
                # policy state while the established watchdog owns the output.
                self.metrics.record_cycle(cycle_started, self.loop_interval)
                return True
            # Age of the telemetry snapshot this cycle decides on (ms)
            grid_age = sys_data.get("_grid_age")
            if grid_age is not None:
                self.metrics.record_age(grid_age * 1000.0)
            _stage("get_system_data")

            self._update_dvcc_limits()
            _stage("dvcc")

            pending_manual = self.manual_setpoint
            if pending_manual is not None:
                self._reset_submeter_trim("manual_control")
                setpoint = pending_manual
                flags = "[MANUAL] "
            else:
                setpoint, flags = self.calculate_setpoint(sys_data)
            _stage("calculate_setpoint")

            self.handle_minimize_charging(sys_data)
            _stage("minimize_charging")

            current_grid = self.victron.get_grid_status()
            if current_grid.get("_grid_selection_generation") != sys_data.get(
                "_grid_selection_generation"
            ) or current_grid.get("_grid_measurement_time") != sys_data.get(
                "_grid_measurement_time"
            ):
                # Recalculate rather than writing across a source/sample edge.
                self._reset_submeter_trim("source_changed_before_write")
                self.metrics.record_cycle(cycle_started, self.loop_interval)
                return True
            if not self._grid_ready_for_control(current_grid):
                self.metrics.record_cycle(cycle_started, self.loop_interval)
                return True
            write_ok = self.dry_run
            previous_for_display = self.previous_setpoint

            def accept_control_setpoint():
                # Commit the applied baseline while the hardware-write lock
                # is held, before another thread can accept a manual override.
                if self._trim_decision is not None:
                    self.submeter_trim.commit(
                        self._trim_decision, now=time.monotonic(), wall_time=time.time()
                    )
                self.previous_setpoint = setpoint
                self._last_backup_measurement = (
                    sys_data.get("_grid_measurement_time") if sys_data.get("_grid_backup") else None
                )

            with self._watchdog._lock:
                if not self._trim_write_ready(current_grid):
                    self._reset_submeter_trim("changed_before_write")
                    self.metrics.record_cycle(cycle_started, self.loop_interval)
                    return True
                # Limits can change after calculation. Share their setter's
                # lock so no automatic write escapes the currently active range.
                bounded_setpoint = max(self.power_limit_min, min(self.power_limit_max, setpoint))
                if bounded_setpoint != setpoint:
                    self._reset_submeter_trim("power_limits_changed")
                    setpoint = bounded_setpoint
                if self.dry_run:
                    flags = f"{C.MAGENTA}[DRY]{C.RESET}" + flags
                    write_ok = self._watchdog.write_control_setpoint(
                        setpoint, generation, dry_run=True, on_accept=accept_control_setpoint
                    )
                else:
                    write_started = time.perf_counter()
                    write_ok = self._watchdog.write_control_setpoint(
                        setpoint, generation, on_accept=accept_control_setpoint
                    )
                    self.metrics.record_write(
                        (time.perf_counter() - write_started) * 1000.0, write_ok
                    )
            if not write_ok:
                self._reset_submeter_trim("write_rejected")
            if generation != self._watchdog.control_generation():
                self._reset_submeter_trim("control_generation")
                self._update_grid_loss_state()
                self.metrics.record_cycle(cycle_started, self.loop_interval)
                return True

            if write_ok and pending_manual is not None and self.manual_setpoint == pending_manual:
                self.manual_setpoint = None

            # Live console is TCP :9999 via broadcast_line below — do not emit
            # GNU screen title escapes (\033k…\033\\) to stdout; under
            # daemontools/multilog they pollute Loki as raw k63/k40/… noise.
            _stage("setpoint_write")

            # Inject cached data for console UI
            sys_data["battery_socs"] = self._cached_battery_socs
            sys_data["mppt_data"] = self._cached_mppt_data
            sys_data["pv_inverter_powers"] = self._cached_pv_powers

            filtered_display = self.filtered_gt if self.filtered_gt is not None else sys_data["gt"]
            line = self.console.format_line(
                sys_data, setpoint, previous_for_display, flags, filtered_display
            )
            self.last_console_line = line
            broadcast_line(line)
            _stage("console_render")

            # Rebuild the full telemetry dict at most every UPDATE_STATE_INTERVAL,
            # not every cycle. It feeds the web UI / MQTT (fresh enough at 2 Hz);
            # the control decision already ran in calculate_setpoint above.
            if time.monotonic() - self._last_update_state_time >= UPDATE_STATE_INTERVAL:
                self.update_state(sys_data, setpoint)
                self._last_update_state_time = time.monotonic()
            _stage("update_state")

            self.metrics.record_cycle(cycle_started, self.loop_interval)
            try:
                if self.get_control_flag("no_feed"):
                    time.sleep(NO_FEED_SLEEP_INTERVAL)
            # Optional failure-path delay must never prevent the next safety cycle.
            except Exception:  # nosec B110
                pass  # Best effort - an optional delay must not stop the control loop
            return True
        except KeyboardInterrupt:
            return False
        except Exception as e:
            try:
                self._reset_submeter_trim("cycle_error")
                log_exception(f"Error in control cycle: {e}")
            finally:
                # Failed iterations must not disappear from latency/deadline stats.
                self.metrics.record_cycle(cycle_started, self.loop_interval)
            return True

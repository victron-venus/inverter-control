"""Main controller for grid-zero feed-in management."""

import logging
import signal
import time
import traceback
from datetime import UTC, datetime
from typing import Any

import inverter_control.config as _config
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
    ENABLE_EV,
    ENABLE_GRID_SMOOTHING_WITH_HOME,
    ENABLE_HA,
    ENABLE_HA_LOADS,
    ENABLE_WATER,
    GRID_FILTER_TAU,
    LOOP_INTERVAL,
    MQTT_SLIM_EXCLUDE_KEYS,
    MQTT_SLIM_STATE,
    NO_FEED_SLEEP_INTERVAL,
    POWER_LIMIT_MAX,
    POWER_LIMIT_MIN,
    WEBHOOK_SERVER_HOST,
    WEBHOOK_SERVER_PORT,
)
from inverter_control.config import (
    Colors as C,
)
from inverter_control.console_server import broadcast_line
from inverter_control.console_ui import ConsoleUI
from inverter_control.dvcc import create_dvcc_from_config
from inverter_control.grid_filter import GridFilter
from inverter_control.homeassistant import get_ha
from inverter_control.logic import SetpointCalculator, SystemState
from inverter_control.metrics import CycleMetrics
from inverter_control.prom_metrics import publish as prom_metrics_publish
from inverter_control.victron import (
    TOU_END_SETTING,
    TOU_START_SETTING,
    get_victron,
)
from inverter_control.watchdog import HardwareWatchdog, WatchdogTimeoutError
from inverter_control.water import WaterSystemReader
from inverter_control.webhook_server import get_webhook_server

logger = logging.getLogger("inverter-control")

# How often the GUI-editable TOU settings are re-read from localsettings
TOU_SETTING_TTL_SECONDS = 60.0


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
        self.ha = get_ha()

        # Water comes from dbus-pump D-Bus services (no HA). In test mode the
        # victron client never touches the bus, so skip the reader entirely.
        self.water: WaterSystemReader | None = None
        if not getattr(self.victron, "_test_mode", False):
            self.water = WaterSystemReader(self.victron.dbus_get)

        # Load UI configuration
        from inverter_control.config import (
            get_ui_config,  # pylint: disable=import-outside-toplevel
        )

        self.ui_config = get_ui_config()

        # Initialize Logic and UI components
        config_dict = {k: getattr(_config, k) for k in _config.EXPORTED_KEYS}
        if GRID_FILTER_TAU > 0:
            # EMA smoothing moves into the background GridFilter thread
            # (time-based tau); logic receives pre-smoothed input, so the
            # per-cycle EMA must be identity to avoid double smoothing.
            config_dict["EMA_ALPHA"] = 1.0
        self.calculator = SetpointCalculator(config_dict)
        self.console = ConsoleUI(self.ha, self.victron, self.water)

        # Background grid EMA filter (owns filtered_gt; started with the main
        # loop). Not started here so unit tests stay single-threaded.
        self.grid_filter: GridFilter | None = None
        if GRID_FILTER_TAU > 0:
            self.grid_filter = GridFilter(
                getter=lambda: float(self.victron.get_ac_in_power()),
                tau=GRID_FILTER_TAU,
            )

        # State
        self.current_setpoint = 0
        self.previous_setpoint = 0
        self.manual_setpoint: int | None = None
        self.delay = 0  # Delay counter for load switching
        self.filtered_gt: float | None = None

        self.loop_count = 0
        self.state: dict[str, Any] = {}
        self.last_console_line = None
        self._solar_forecast: dict[str, Any] | None = None

        # Pre-charge state (triggered by solar-forecast webhook)
        self._pre_charge_requested = False
        self._pre_charge_horizon_hours = 6

        # TOU window settings cache (see _tou_hours)
        self._tou_cache: tuple[int, int] | None = None
        self._tou_cache_time = 0.0
        if not getattr(self.victron, "_test_mode", False):
            self.victron.ensure_tou_settings(
                _config.TOU_EXPENSIVE_START_HOUR, _config.TOU_EXPENSIVE_END_HOUR
            )

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

        # Dynamic settings (overridable)
        self.power_limit_min = POWER_LIMIT_MIN
        self.power_limit_max = POWER_LIMIT_MAX
        self.loop_interval = LOOP_INTERVAL
        # Rolling latency metrics for hardware-run benchmarking (see metrics.py)
        self.metrics = CycleMetrics()
        self._last_perf_snapshot = 0.0

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
            timeout_seconds=30,
            check_interval=5.0,
            dry_run=self.dry_run,
            get_setpoint=lambda: self.previous_setpoint,
        )

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

    def _handle_pre_charge_webhook(self, payload: dict) -> bool:
        """Handle pre-charge webhook from solar-forecast-langgraph.

        Sets internal flag to trigger pre-charge on next control cycle.
        The charge_battery strategy in logic.py will handle the actual
        setpoint calculation (forces ~2200W charging).
        """
        try:
            from inverter_control.mqtt_bridge import (  # pylint: disable=import-outside-toplevel
                get_mqtt_bridge,
            )

            forecast_wh = payload.get("forecast_energy_wh", 0)
            threshold_wh = payload.get("threshold_wh", 0)
            horizon_hours = payload.get("horizon_hours", 6)
            logger.info(
                f"Pre-charge webhook: forecast={forecast_wh:.0f}Wh "
                f"threshold={threshold_wh:.0f}Wh horizon={horizon_hours}h"
            )
            bridge = get_mqtt_bridge()

            if self._in_expensive_window():
                logger.info("Pre-charge webhook ignored: expensive grid window active")
                if bridge:
                    bridge.publish_notification(
                        notification_id="precharge-suppressed-"
                        + datetime.now(UTC).strftime("%Y%m%d-%H"),
                        level="info",
                        title="Pre-charge skipped",
                        body=(
                            "Expensive grid window "
                            f"({_config.TOU_EXPENSIVE_START_HOUR}:00"
                            f"-{_config.TOU_EXPENSIVE_END_HOUR}:00)"
                        ),
                        source="inverter-control",
                    )
                return True

            # Set pre-charge flag - this will be picked up in run_cycle
            # by setting the HA boolean 'charge_battery' or by overriding
            # the state.charge_battery flag directly
            self._pre_charge_requested = True
            self._pre_charge_horizon_hours = horizon_hours

            # Notify dashboards (id is hour-scoped so consumers can dedupe)
            if bridge:
                notification_id = "precharge-" + datetime.now(UTC).strftime("%Y%m%d-%H")
                bridge.publish_notification(
                    notification_id=notification_id,
                    level="info",
                    title="Pre-charge triggered",
                    body=(
                        f"Low solar forecast: {forecast_wh / 1000:.1f} kWh "
                        f"< {threshold_wh / 1000:.1f} kWh in {horizon_hours}h"
                    ),
                    source="solar-forecast",
                )
            return True
        except Exception:
            logger.exception("Error handling pre-charge webhook")
            return False

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
        return self.state

    def set_manual_setpoint(self, value: int) -> bool:
        self.manual_setpoint = max(self.power_limit_min, min(self.power_limit_max, value))
        return True

    def calculate_setpoint(self, sys_data: dict[str, Any]) -> tuple[int, str]:
        """Orchestrate state collection and delegate calculation to logic.py"""

        # Prepare SystemState snapshot
        mppt_data = self.victron.get_mppt_data()
        mppt_total = sum(m["w"] for m in mppt_data.values())
        tasmota_powers = self.victron.get_pv_power()
        tasmota_total = sum(tasmota_powers)

        # Cache these reads so update_state doesn't re-query D-Bus this cycle
        self._cached_mppt_data = mppt_data
        self._cached_pv_powers = tasmota_powers

        # Grid smoothing with Home total (Vue via D-Bus)
        # derived_gt = home_total - pv_total (negative = export, positive = import)
        # Blend with instantaneous CT meter for stable control
        home_total = 0.0
        derived_gt = None
        if ENABLE_GRID_SMOOTHING_WITH_HOME:
            home_total = self.ha.get_vue_sensor("total", 0)
            if home_total > 0:
                pv_total = mppt_total + tasmota_total
                derived_gt = home_total - pv_total

        # Handle pre-charge request from solar forecast webhook
        charge_battery = self.ha.get_boolean("charge_battery")
        if self._pre_charge_requested:
            self._pre_charge_requested = False  # One-shot
            if self._in_expensive_window():
                logger.info("Pre-charge suppressed: expensive grid window active")
            else:
                charge_battery = True
                logger.info("Pre-charge triggered by solar forecast")

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
            home_total=home_total,
            only_charging=self.ha.get_boolean("only_charging"),
            no_feed=self.ha.get_boolean("no_feed"),
            house_support=self.ha.get_boolean("house_support"),
            charge_battery=charge_battery,
            do_not_supply_charger=self.ha.get_boolean("do_not_supply_charger"),
            limit_to_ev=self.ha.get_boolean("set_limit_to_ev_charger"),
            previous_setpoint=self.previous_setpoint,
            filtered_gt=(self.grid_filter.value() if self.grid_filter else self.filtered_gt),
            derived_gt=derived_gt,
        )

        # Perform calculation
        result = self.calculator.calculate(state)

        # Mirror for display/MQTT state; the authoritative smoothed value
        # lives in the GridFilter thread when it is running.
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
        return {
            "ev_power": self.ha.get_vue_sensor("ev_charger", 0),
            "car_soc": self.ha.get_sensor("car_soc", 0),
            "ev_charging_kw": self.ha.get_sensor("ev_charging_power", 0),
        }

    def _get_water_state(self) -> dict[str, Any]:
        if not ENABLE_WATER:
            return {"water_level": None, "water_valve": None, "pump_switch": None}
        if self.water is None:
            # Test mode / no D-Bus: report no data rather than fake zeros
            return {"water_level": None, "water_valve": None, "pump_switch": None}
        return self.water.read()

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
        # All daily stats now from D-Bus (no HA dependency)
        battery_in, battery_out = self.victron.get_battery_daily_energy()
        battery_in_y, battery_out_y = self.victron.get_battery_yesterday_energy()
        mppt_daily = self.victron.get_mppt_daily_yields()
        tasmota_daily = self.victron.get_pv_inverter_daily_yields()
        produced_today = sum(mppt_daily) + sum(tasmota_daily)
        mppt_yesterday = self.victron.get_mppt_yesterday_yields()
        tasmota_yesterday = self.victron.get_pv_inverter_yesterday_yields()
        produced_yesterday = sum(mppt_yesterday) + sum(tasmota_yesterday)

        return {
            "produced_today": produced_today,
            "produced_yesterday": produced_yesterday,
            "produced_dollars": 0.0,  # No HA - compute locally if needed
            "grid_kwh": 0.0,  # No D-Bus equivalent yet
            "battery_in": battery_in,
            "battery_out": battery_out,
            "battery_in_yesterday": battery_in_y,
            "battery_out_yesterday": battery_out_y,
            "pv_total_daily": produced_today,
            "tasmota_daily": tasmota_daily,
            "mppt_daily": mppt_daily,
            "tasmota_yesterday": tasmota_yesterday,
            "mppt_yesterday": mppt_yesterday,
        }

    def update_state(self, sys_data: dict[str, Any], setpoint: int):
        # mppt/tasmota data was already read this cycle in calculate_setpoint
        self._cached_battery_socs = self.victron.get_battery_chain_socs()
        _, self._cached_inv_state = self.victron.get_inverter_state()

        # Inject cached data into sys_data for console UI use
        sys_data["mppt_data"] = self._cached_mppt_data
        sys_data["tasmota_powers"] = self._cached_pv_powers
        sys_data["battery_socs"] = self._cached_battery_socs

        # Full state for web UI
        self.state = {
            **sys_data,
            "setpoint": setpoint,
            "filtered_gt": self.filtered_gt,
            "dry_run": self.dry_run,
            "mppt_total": sum(m["w"] for m in self._cached_mppt_data.values()),
            "tasmota_total": sum(self._cached_pv_powers),
            "solar_total": sum(m["w"] for m in self._cached_mppt_data.values())
            + sum(self._cached_pv_powers),
            "mppt_data": self._cached_mppt_data,
            "mppt_individual": [m["w"] for m in self._cached_mppt_data.values()],
            "tasmota_individual": self._cached_pv_powers,
            "inverter_state": self._cached_inv_state,
            "battery_socs": self._cached_battery_socs,
            "batteries": self._get_cached_batteries(),
            "mppt_chargers": self._get_cached_mppt_chargers(),
            **self._get_ev_state(),
            **self._get_water_state(),
            **self._get_ha_state(),
            "loads": self.victron.get_acload_powers() if ENABLE_HA_LOADS else {},
            "ess_mode": self.victron.get_ess_mode(),
            "battery_power": sys_data.get("bp", 0),
            "battery_voltage": sys_data.get("bv", 0),
            "battery_current": sys_data.get("bc", 0),
            "battery_soc": sys_data.get("soc", 0) or self.victron.get_battery_soc_local(sys_data),
            "daily_stats": self._get_daily_stats(),
            "solar_forecast": self._solar_forecast,
            "limits": {"min": self.power_limit_min, "max": self.power_limit_max},
            "loop_interval": self.loop_interval,
            "version": VERSION,
            "uptime": int(time.time() - self._start_time),
            "ui_config": self.ui_config,
            "dvcc_limits": self.dvcc_limits if self.dvcc_limits else None,
        }
        # Perf snapshot into state at most every 5s (percentile sort is cheap
        # but pointless at 3 Hz)
        now = time.time()
        if now - self._last_perf_snapshot >= 5.0:
            self.metrics.sample_process()
            self.state["perf"] = self.metrics.snapshot()
            prom_metrics_publish(self.state["perf"])
            self._last_perf_snapshot = now

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
        cycle_started = time.monotonic()
        stage_started = time.perf_counter()

        def _stage(name: str) -> None:
            """Record duration of the stage that just ended."""
            nonlocal stage_started
            now = time.perf_counter()
            self.metrics.record_stage(name, (now - stage_started) * 1000.0)
            stage_started = now

        try:
            self.last_console_line = None
            sys_data = self.victron.get_system_data()
            # Mark D-Bus telemetry as fresh for hardware watchdog
            self._watchdog.mark_dbus_update()
            # Age of the telemetry snapshot this cycle decides on (ms)
            last_update = sys_data.get("_last_update")
            if last_update:
                self.metrics.record_age((time.time() - last_update) * 1000.0)
            _stage("get_system_data")

            if self.dvcc_calculator is not None:
                now = time.time()
                if now - self._last_cell_data_time > 30:
                    battery_data = self.victron.get_battery_cell_data()
                    self._cached_battery_cell_data = battery_data
                    self._last_cell_data_time = now
                if self._cached_battery_cell_data is not None:
                    self.dvcc_limits = self.dvcc_calculator.calculate(
                        self._cached_battery_cell_data
                    )
            _stage("dvcc")

            if self.manual_setpoint is not None:
                setpoint = self.manual_setpoint
                flags = "[MANUAL] "
                self.manual_setpoint = None
            else:
                setpoint, flags = self.calculate_setpoint(sys_data)
            _stage("calculate_setpoint")

            self.handle_minimize_charging(sys_data)
            _stage("minimize_charging")

            if self.dry_run:
                flags = f"{C.MAGENTA}[DRY]{C.RESET}" + flags
            else:
                write_started = time.perf_counter()
                write_ok = self.victron.set_grid_setpoint(setpoint)
                self.metrics.record_write((time.perf_counter() - write_started) * 1000.0, write_ok)
                # Mark setpoint-write liveness for the hardware watchdog
                self._watchdog.mark_setpoint_update()

            print(f"\033k{sys_data['gt']}\033\\", end="")
            _stage("setpoint_write")

            # Inject cached data for console UI
            sys_data["battery_socs"] = self._cached_battery_socs
            sys_data["mppt_data"] = self._cached_mppt_data
            sys_data["tasmota_powers"] = self._cached_pv_powers

            filtered_display = self.filtered_gt if self.filtered_gt is not None else sys_data["gt"]
            line = self.console.format_line(
                sys_data, setpoint, self.previous_setpoint, flags, filtered_display
            )
            self.last_console_line = line
            print(line)
            broadcast_line(line)
            _stage("console_render")

            self.update_state(sys_data, setpoint)
            self.console.update_terminal_title()
            self.previous_setpoint = setpoint
            _stage("update_state")

            self.metrics.record_cycle(cycle_started, self.loop_interval)
            try:
                if self.ha.get_boolean("no_feed"):
                    time.sleep(NO_FEED_SLEEP_INTERVAL)
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

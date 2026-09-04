#!/usr/bin/env python3
"""
Inverter Control Configuration
All configurable parameters in one place
"""

import functools
import os
import subprocess

# =============================================================================
# LOCAL CONFIG (imported from local_config.py - not tracked by git)
# =============================================================================
try:
    from local_config import (  # pylint: disable=unused-import
        HA_DUMP_LOADS,
        HA_SENSORS,
        HA_TOKEN,
        HA_URL,
        VUE_SENSORS,
    )
except ImportError:
    # Fallback for development or if local_config.py doesn't exist
    print("WARNING: local_config.py not found! Copy local_config.example.py to local_config.py")
    HA_URL = "http://localhost:8123"  # nosec B310 — local dev fallback
    HA_TOKEN = "your_token_here"
    HA_SENSORS = {}
    VUE_SENSORS = {}
    HA_DUMP_LOADS = []


def _import_local_config(name: str, default=""):
    """Import a variable from local_config with a fallback default."""
    try:
        import local_config

        return getattr(local_config, name, default)
    except (ImportError, AttributeError):
        return default


# =============================================================================
# WATER SYSTEM (dbus-pump D-Bus services on the GX - no Home Assistant)
# =============================================================================
# dbus-pump exposes com.victronenergy.tank.ha_tank{N} and
# com.victronenergy.pump.startstop{N}; these must match its local_config.py.
WATER_TANK_INSTANCE = int(_import_local_config("WATER_TANK_INSTANCE", 21))
WATER_PUMP_INSTANCE = int(_import_local_config("WATER_PUMP_INSTANCE", 1))
WATER_VALVE_INSTANCE = int(_import_local_config("WATER_VALVE_INSTANCE", 2))

# =============================================================================
# EV CHARGER / VEHICLE (dbus-evcharger + dbus-ev D-Bus services on the GX)
# =============================================================================
# dbus-evcharger exposes com.victronenergy.evcharger.<N> (wallbox).
# dbus-ev exposes com.victronenergy.ev.<suffix> (vehicle, has /Soc /VIN).
# Both are autodetected via D-Bus; these are fallback defaults only.
EV_INSTANCE = int(_import_local_config("EV_INSTANCE", 22))
EVCHARGER_INSTANCE = int(_import_local_config("EVCHARGER_INSTANCE", 40))

# =============================================================================
# OPTIONAL FEATURES
# =============================================================================
# Set to False to disable features manually, or leave True for auto-detection.
# HA-backed features auto-disable if HA_TOKEN is not configured.
#
# When disabled:
#   - Console output omits the corresponding sections
#   - Web UI hides the corresponding cards
#   - No HA API calls are made for disabled features

ENABLE_EV = True  # EV charging monitoring (car SoC, wallbox power) via D-Bus
ENABLE_WATER = True  # Water level, pump and valve (via dbus-pump D-Bus)
ENABLE_ACLOADS = True  # AC load monitoring via D-Bus acload services
ENABLE_HA = True  # Home Assistant integration (net_usage sensor, dump load control)

# Auto-disable all HA features if no valid token configured
if HA_TOKEN in ("", "your_token_here", None):
    ENABLE_HA = False
    ENABLE_ACLOADS = False
    # EV and WATER are D-Bus based and do NOT require Home Assistant
    print("INFO: Home Assistant disabled (no valid HA_TOKEN in local_config.py)")

# =============================================================================
# MQTT BRIDGE (for remote web dashboard)
# =============================================================================
# MQTT broker address (for remote dashboard connection)
# Set to empty string to disable MQTT bridge
MQTT_BROKER = "localhost"  # Venus OS has built-in MQTT broker
MQTT_PORT = 1883
MQTT_TOPIC_PREFIX = "inverter"

# =============================================================================
# ALERT STORAGE
# =============================================================================
# Path to JSON file for persistent alert storage
ALERT_STORAGE_PATH = _import_local_config(
    "ALERT_STORAGE_PATH", "/data/inverter_control_alerts.json"
)

# When True, inverter/state MQTT payload omits fields that already live on Victron
# Cerbo MQTT / dbus-* services (grid, consumption, battery bank, solar, loads, EV,
# water, setpoint/mode mirrors). Desktop and other Cerbo-first UIs read those live
# tiles from Cerbo; the daemon must not republish them into inverter/state.
# Keep daemon-only extras: daily_stats, solar_forecast, booleans, features,
# ess_mode, dry_run, ui_config, version/uptime, ha_* flags, filtered_gt, limits,
# loop_interval, dvcc_limits, perf.
MQTT_SLIM_STATE = True

MQTT_SLIM_EXCLUDE_KEYS = frozenset(
    {
        # Grid / consumption (systemcalc + vebus on Cerbo)
        "g1",
        "g2",
        "gt",
        "t1",
        "t2",
        "tt",
        # Battery bank (SmartShunt / system on Cerbo); short + long keys
        "bv",
        "bc",
        "bp",
        "battery_soc",
        "battery_power",
        "battery_voltage",
        "battery_current",
        "battery_socs",
        "batteries",
        # Solar / MPPT / PV inverter (Cerbo solarcharger + pvinverter)
        "solar_total",
        "pv_total",
        "mppt_total",
        "mppt_data",
        "mppt_individual",
        "mppt_chargers",
        "pv_inverter_total",
        "pv_inverter_individual",
        "pv_inverter_powers",
        "pv_inverters",
        # Active loads (dbus-emporia-vue / Cerbo acload)
        "loads",
        # VE.Bus mode + Hub4 setpoint (Cerbo vebus)
        "setpoint",
        "inverter_state",
        # EV (dbus-ev / dbus-evcharger on Cerbo)
        "ev_power",
        "car_soc",
        "ev_charging_kw",
        # Water (dbus-pump / Cerbo tank+startstop)
        "water_level",
        "water_valve",
        "pump_switch",
        # Internal cache stamp from get_system_data (not for consumers)
        "_last_update",
    }
)

# =============================================================================
# WEBHOOK SERVER (for solar forecast pre-charge triggers)
# =============================================================================
WEBHOOK_SERVER_HOST = "0.0.0.0"
WEBHOOK_SERVER_PORT = 8081

# =============================================================================
# TIME-OF-USE EXPENSIVE WINDOW (local hours)
# Forecast pre-charge is suppressed while inside the window so the battery
# never force-charges from expensive grid power. Set both to -1 to disable.
# Handles windows that wrap midnight (e.g. 22 -> 6).
# Resolution order: local_config.py -> environment variable -> default.
# =============================================================================


def _tou_hour(name: str, default: int) -> int:
    """Read a TOU window hour from local_config, then env, then default."""
    raw = _import_local_config(name, None)
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


TOU_EXPENSIVE_START_HOUR = _tou_hour("TOU_EXPENSIVE_START_HOUR", 15)
TOU_EXPENSIVE_END_HOUR = _tou_hour("TOU_EXPENSIVE_END_HOUR", 24)

# =============================================================================
# RUNTIME MODE
# =============================================================================

# Dry-run mode: if True, don't send commands to Victron (safe for testing)
# Can be toggled via web interface at runtime
DRY_RUN = False  # Live mode - sending commands to Victron

# =============================================================================
# VICTRON SYSTEM
# =============================================================================

# =============================================================================
# VICTRON PORTAL ID (auto-detected, no config needed)
# =============================================================================


@functools.lru_cache(maxsize=1)
def _detect_portal_id() -> str:
    """Resolve the VRM Portal ID at runtime.

    Order of resolution:
      1. /sbin/get-unique-id (official Venus OS utility; reads /data/venus/uniqueid)
      2. eth0 MAC address, colons stripped (the classic VRM Portal ID definition)
      3. PORTAL_ID environment variable (override for unusual hardware)
      4. Placeholder stub, so non-Venus systems (tests, local dev) still work
    """
    try:
        portal_id = subprocess.check_output(["/sbin/get-unique-id"], text=True, timeout=5).strip()
        if portal_id:
            return portal_id
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open("/sys/class/net/eth0/address", encoding="utf-8") as f:
            portal_id = f.read().strip().replace(":", "").lower()
            if portal_id:
                return portal_id
    except OSError:
        pass
    portal_id = os.environ.get("PORTAL_ID")
    if portal_id:
        return portal_id
    return "your_portal_id"


def __getattr__(name: str):
    """Lazy module attribute (PEP 562): PORTAL_ID resolved on first access,
    so importing config never blocks on /sbin/get-unique-id (5s timeout)."""
    if name == "PORTAL_ID":
        return _detect_portal_id()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Power limits for outlet protection (Watts)
POWER_LIMIT_MAX = 2250  # Maximum feed-in (positive = charging battery)
POWER_LIMIT_MIN = -2300  # Maximum export (negative = discharging to grid)

# Control loop timing
LOOP_INTERVAL = 0.33  # seconds (3 times per second)
HA_POLL_INTERVAL = 1.5  # seconds for Home Assistant polling
NO_FEED_SLEEP_INTERVAL = 1.0  # seconds to sleep in no_feed mode (slows loop to ~1 Hz)

# Internal hardware watchdog: forces a 0W grid setpoint when BOTH setpoint
# writes and D-Bus telemetry have been silent this long (stall/crash failsafe).
WATCHDOG_TIMEOUT_SECONDS = int(_import_local_config("WATCHDOG_TIMEOUT_SECONDS", 30))
WATCHDOG_CHECK_INTERVAL = float(_import_local_config("WATCHDOG_CHECK_INTERVAL", 5.0))

# Persistent native D-Bus connection (dbus_fast) for Get/Set calls, replacing
# per-call dbus-send subprocesses in the hot path. Automatically falls back
# to dbus-send when the connection is unavailable. Set env USE_NATIVE_DBUS=0
# to force the CLI path.
USE_NATIVE_DBUS = os.environ.get("USE_NATIVE_DBUS", "1").lower() not in ("0", "false")

# Grid zero targeting - Stability tuning for VM-3P75CT or similar fast CT meters
GRID_ZERO_DEADBAND_LOW = -50  # Watts - lower bound (slight export OK)
GRID_ZERO_DEADBAND_HIGH = 30  # Watts - upper bound (slight import OK)
DAMPING_FACTOR = 0.7  # Damping for import correction (0.0-1.0)
EMA_ALPHA = float(
    _import_local_config("EMA_ALPHA", 0.3)
)  # EMA smoothing (0.1=smooth, 0.5=responsive)
# Time constant (seconds) for the background GridFilter thread that owns the
# grid EMA (see inverter_control/grid_filter.py). When > 0, the control loop
# receives pre-smoothed values and EMA_ALPHA is forced to 1.0 to avoid double
# smoothing. Set to 0 to disable the thread and use per-cycle EMA_ALPHA again.
GRID_FILTER_TAU = float(_import_local_config("GRID_FILTER_TAU", 2.0))
SETPOINT_DELTA_LIMIT = 2000  # Maximum change in setpoint per cycle (Watts)

# Aggressive Grid Smoothing with Home Load (Vue via HA cloud)
# Uses "home_total" (total house consumption from Vue) + known production
# to derive a stable grid estimate. Blends with instantaneous CT meter.
# Values are tuned by inverter-monitoring analysis/grid_correlation.py.
ENABLE_GRID_SMOOTHING_WITH_HOME = bool(
    _import_local_config("ENABLE_GRID_SMOOTHING_WITH_HOME", False)
)
GRID_SMOOTHING_HOME_WEIGHT = float(_import_local_config("GRID_SMOOTHING_HOME_WEIGHT", 0.7))
GRID_SMOOTHING_DERIVED_ALPHA = float(_import_local_config("GRID_SMOOTHING_DERIVED_ALPHA", 0.1))
# Time constant (seconds) for the background GridFilter thread that smooths
# derived_gt (home_total - pv_total), giving one consistent notion of smoothed
# grid alongside the CT filter. When > 0 it replaces the legacy per-cycle
# GRID_SMOOTHING_DERIVED_ALPHA EMA inside SetpointCalculator; set to 0 to
# fall back to the legacy path without a version downgrade.
GRID_SMOOTHING_DERIVED_TAU = float(_import_local_config("GRID_SMOOTHING_DERIVED_TAU", 3.2))

# Loud warning when live control is silently ignored: GX only honors
# /Hub4/L1/AcPowerSetpoint while ESS is in External control (Hub4Mode=3).
# Warn after the mismatch persists this many minutes.
ESS_EXTERNAL_WARN_MINUTES = float(_import_local_config("ESS_EXTERNAL_WARN_MINUTES", 5))

# Export asymmetry — export to grid is undesirable, correct more aggressively
EXPORT_DAMPING = 1.0  # Full correction for export (no damping)

# Burst correction — immediate response to sudden load spikes (e.g. pump startup)
# When |gt - filtered_gt| exceeds threshold, apply direct correction bypassing EMA lag
BURST_THRESHOLD = 150  # Watts — minimum spike to trigger burst correction
BURST_GAIN = 0.8  # Fraction of spike to correct immediately (0.0–1.0)

# D-term: prevent overshoot when gt converges to zero fast
# When gt is close to zero but still moving quickly, brake to avoid crossing zero
D_BRAKE_ZONE = 100  # Watts — how close to zero to start braking
D_THRESHOLD = 50  # Watts/cycle — minimum derivative to trigger braking
D_GAIN = 0.3  # Fraction of derivative to apply as brake (0.0–1.0)

# Creep correction — slow drift fix when grid stays in deadband but offset from zero
CREEP_RATE = 0.5  # Watts accumulated per cycle while in deadband
CREEP_MAX = 100.0  # Maximum creep correction (Watts)

# Solar output offset - reduce output by this amount to avoid grid export
# Used in only_charging, do_not_supply_charger, and other solar-limited modes
SOLAR_OUTPUT_OFFSET = 60  # Watts

# Inverter efficiency - DC to AC conversion losses
# MPPT produces DC 48V, inverter converts to AC 110V with ~92-95% efficiency
# Example: 2000W DC from MPPT → ~1850W AC output (at 92.5%)
INVERTER_EFFICIENCY = 0.94  # 94% efficiency (adjust based on your system)

# =============================================================================
# HOME ASSISTANT
# =============================================================================

# HA_URL, HA_TOKEN, HA_SENSORS, VUE_SENSORS,
# HA_DUMP_LOADS are all imported from local_config.py

HA_TIMEOUT = 2.0  # seconds

# Timezone for console output
TIMEZONE = "America/Los_Angeles"

# =============================================================================
# DVCC (Dynamic Voltage & Current Control) - Battery Protection
# =============================================================================
# These settings control automatic charge/discharge current limiting based on:
# - Cell voltages (prevent over/under voltage)
# - Cell imbalance (give balancers time to work)
# - Temperatures (prevent charging below 0°C or above 50°C)
# - SoC limits (extend battery life at extremes)
#
# When enabled, inverter-control will read battery data from D-Bus and
# calculate safe CCL/DCL limits. These are published via MQTT for the dashboard
# and can be used to set Victron DVCC limits if Victron's DVCC is enabled.

# Enable DVCC calculation (requires D-Bus battery data from dbus-mqtt-battery)
DVCC_ENABLED = True

# DVCC Cell Voltage Thresholds (LiFePO4 typical values)
DVCC_CELL_FULL_CURRENT = 3.40  # V - Below this: 100% charge current
DVCC_CELL_START_LIMIT = 3.45  # V - Start reducing charge current
DVCC_CELL_BALANCE_VOLTAGE = 3.50  # V - Aggressive reduction for balancing
DVCC_CELL_NEAR_FULL = 3.55  # V - Minimal current (tail charge)
DVCC_CELL_CUTOFF = 3.60  # V - Stop charging completely (BMS cutoff protection)

# DVCC Maximum Currents
DVCC_MAX_CHARGE_CURRENT = 100.0  # A - Max charge current at normal conditions
DVCC_MAX_DISCHARGE_CURRENT = 120.0  # A - Max discharge current
DVCC_MIN_CHARGE_CURRENT = 2.0  # A - Minimum tail charge current (for balancing)

# DVCC Cell Imbalance Protection
DVCC_IMBALANCE_START_LIMIT = 0.05  # V - Start reducing if delta > this
DVCC_IMBALANCE_AGGRESSIVE = 0.10  # V - Aggressive reduction
DVCC_IMBALANCE_CRITICAL = 0.20  # V - Minimal current only

# DVCC Temperature Limits (°C) - LiFePO4 safe range
DVCC_TEMP_REDUCED = 5  # °C - Below this, charge current is heavily reduced
DVCC_TEMP_FULL_CURRENT_MIN = 10  # °C - Full current above this temp
DVCC_TEMP_FULL_CURRENT_MAX = 40  # °C - Full current below this temp
DVCC_TEMP_STOP_CHARGE = 0  # °C - Stop charging below (lithium plating risk)
DVCC_TEMP_STOP_CHARGE_HIGH = 50  # °C - Stop charging above
DVCC_TEMP_DISCHARGE_MIN = -20  # °C - Stop discharging below
DVCC_TEMP_DISCHARGE_REDUCED = -10  # °C - Reduced discharge below

# DVCC SoC-based Current Reduction (optional, for battery longevity)
DVCC_SOC_REDUCE_START = 95  # % - Start reducing charge current above this SoC
DVCC_SOC_REDUCE_FACTOR = 0.5  # Factor at 100% SoC (0.5 = 50% of max current)
DVCC_SOC_DISCHARGE_STOP = 5  # % - Stop discharging at this SoC
DVCC_SOC_DISCHARGE_REDUCED = 15  # % - Reduce discharge below this SoC

# DVCC Rate Limiting (smooth transitions)
DVCC_CCL_CHANGE_RATE = 10.0  # Max CCL change per second (A/s)
DVCC_DCL_CHANGE_RATE = 15.0  # Max DCL change per second (A/s)

# DVCC Cell Max Voltage (for CVL calculation)
DVCC_CELL_MAX_VOLTAGE = 3.65  # V - Max cell voltage for CVL
DVCC_CELLS_PER_BMS = 4  # Cells per BMS module

# =============================================================================
# INVERTER STATES (VE.Bus)
# =============================================================================

INVERTER_STATES = {
    0: "Off",
    1: "Low Power",
    2: "Fault",
    3: "Bulk",
    4: "Absorption",
    5: "Float",
    6: "Storage",
    7: "Equalize",
    8: "Passthru",
    9: "Inverting",
    10: "Power assist",
    11: "Power supply",
    252: "External control",
}

# =============================================================================
# CONSOLE COLORS (ANSI)
# =============================================================================


class Colors:
    """ANSI color codes for terminal output"""

    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


# =============================================================================
# EXPORTED KEYS - used by SetpointCalculator
# =============================================================================
EXPORTED_KEYS = [
    "BURST_GAIN",
    "BURST_THRESHOLD",
    "CREEP_MAX",
    "CREEP_RATE",
    "D_BRAKE_ZONE",
    "D_GAIN",
    "D_THRESHOLD",
    "DAMPING_FACTOR",
    "EMA_ALPHA",
    "EXPORT_DAMPING",
    "GRID_SMOOTHING_DERIVED_ALPHA",
    "GRID_SMOOTHING_DERIVED_TAU",
    "GRID_SMOOTHING_HOME_WEIGHT",
    "GRID_ZERO_DEADBAND_HIGH",
    "GRID_ZERO_DEADBAND_LOW",
    "INVERTER_EFFICIENCY",
    "POWER_LIMIT_MAX",
    "POWER_LIMIT_MIN",
    "SETPOINT_DELTA_LIMIT",
    "SOLAR_OUTPUT_OFFSET",
]

# =============================================================================
# STARTUP CONFIG VALIDATION
# =============================================================================
# Catch type mismatches early (e.g. HA_TOKEN = 123 instead of str)
# so the service fails fast at import time, not at runtime.


def _check_type(name: str, value, expected) -> str | None:
    if not isinstance(value, expected):
        return f"{name} must be {expected if isinstance(expected, type) else '/'.join(t.__name__ for t in expected)}, got {type(value).__name__}"
    return None


def _check_range(name: str, value, lo, hi) -> str | None:
    if not isinstance(value, (int, float)):
        return None  # type check already handles this
    if not lo <= value <= hi:
        return f"{name} must be {lo}-{hi}, got {value!r}"
    return None


def _validate_config():
    """Validate critical config values at import time."""
    checks = [
        _check_type("HA_TOKEN", HA_TOKEN, str),
        _check_type("HA_URL", HA_URL, str),
        # PORTAL_ID not checked: lazily resolved via __getattr__, always str
        _check_type("HA_SENSORS", HA_SENSORS, dict),
        _check_type("VUE_SENSORS", VUE_SENSORS, dict),
        _check_type("HA_DUMP_LOADS", HA_DUMP_LOADS, (list, tuple)),
        _check_type("LOOP_INTERVAL", LOOP_INTERVAL, (int, float)),
        _check_type("POWER_LIMIT_MAX", POWER_LIMIT_MAX, (int, float)),
        _check_type("POWER_LIMIT_MIN", POWER_LIMIT_MIN, (int, float)),
        _check_type("DAMPING_FACTOR", DAMPING_FACTOR, (int, float)),
        _check_type("EMA_ALPHA", EMA_ALPHA, (int, float)),
        _check_range("DAMPING_FACTOR", DAMPING_FACTOR, 0.0, 1.0),
        _check_range("EMA_ALPHA", EMA_ALPHA, 0.0, 1.0),
        _check_type("GRID_SMOOTHING_HOME_WEIGHT", GRID_SMOOTHING_HOME_WEIGHT, (int, float)),
        _check_range("GRID_SMOOTHING_HOME_WEIGHT", GRID_SMOOTHING_HOME_WEIGHT, 0.0, 1.0),
        _check_type("GRID_SMOOTHING_DERIVED_ALPHA", GRID_SMOOTHING_DERIVED_ALPHA, (int, float)),
        _check_range("GRID_SMOOTHING_DERIVED_ALPHA", GRID_SMOOTHING_DERIVED_ALPHA, 0.0, 1.0),
        _check_type("GRID_SMOOTHING_DERIVED_TAU", GRID_SMOOTHING_DERIVED_TAU, (int, float)),
        _check_type("GRID_FILTER_TAU", GRID_FILTER_TAU, (int, float)),
        _check_type("WATCHDOG_TIMEOUT_SECONDS", WATCHDOG_TIMEOUT_SECONDS, int),
        _check_type("WATCHDOG_CHECK_INTERVAL", WATCHDOG_CHECK_INTERVAL, (int, float)),
        _check_type("ESS_EXTERNAL_WARN_MINUTES", ESS_EXTERNAL_WARN_MINUTES, (int, float)),
    ]

    if GRID_SMOOTHING_DERIVED_TAU < 0:
        checks.append(
            f"GRID_SMOOTHING_DERIVED_TAU must be >= 0, got {GRID_SMOOTHING_DERIVED_TAU!r}"
        )

    if GRID_FILTER_TAU < 0:
        checks.append(f"GRID_FILTER_TAU must be >= 0, got {GRID_FILTER_TAU!r}")

    if WATCHDOG_TIMEOUT_SECONDS <= 0:
        checks.append(
            f"WATCHDOG_TIMEOUT_SECONDS must be positive, got {WATCHDOG_TIMEOUT_SECONDS!r}"
        )
    if WATCHDOG_CHECK_INTERVAL <= 0:
        checks.append(f"WATCHDOG_CHECK_INTERVAL must be positive, got {WATCHDOG_CHECK_INTERVAL!r}")
    if ESS_EXTERNAL_WARN_MINUTES <= 0:
        checks.append(
            f"ESS_EXTERNAL_WARN_MINUTES must be positive number, got {ESS_EXTERNAL_WARN_MINUTES!r}"
        )

    if LOOP_INTERVAL <= 0:
        checks.append(f"LOOP_INTERVAL must be positive number, got {LOOP_INTERVAL!r}")

    errors = [c for c in checks if c is not None]
    if errors:
        msg = "Configuration errors (fix local_config.py):\n  - " + "\n  - ".join(errors)
        raise ValueError(msg)


_validate_config()


# =============================================================================
# UI CONFIGURATION (moved from ui_config.py)
# =============================================================================

UI_CONFIG: dict = {
    "batteries": [
        {
            "id": "chain1",
            "name": "JBD Chain 1",
            "show_current": True,
            "show_power": True,
        },
        {
            "id": "chain2",
            "name": "JBD Chain 2",
            "show_current": True,
            "show_power": True,
        },
        {
            "id": "virtual",
            "name": "Virtual Battery",
            "show_current": True,
            "show_power": True,
        },
    ],
    "solar_sources": {
        "mppt_names": {0: "MPPT-290", 1: "MPPT-291", 2: "MPPT-292"},
        "pv_inverters": [
            {"id": "pv1", "name": "PV Inverter 1", "index": 0},
            {"id": "pv2", "name": "PV Inverter 2", "index": 1},
        ],
    },
    "loads": {
        "hidden": ["solar_shed"],
        "min_watts": 10,
    },
    "water": {
        "tank_instance": WATER_TANK_INSTANCE,
        "pump_instance": WATER_PUMP_INSTANCE,
        "valve_instance": WATER_VALVE_INSTANCE,
    },
    "ev": {
        "instance": EV_INSTANCE,
        "evcharger_instance": EVCHARGER_INSTANCE,
        "soc_path": "/Soc",
        "power_path": "/Ac/Power",
    },
}


def get_ui_config() -> dict:
    """Get UI configuration"""
    return UI_CONFIG

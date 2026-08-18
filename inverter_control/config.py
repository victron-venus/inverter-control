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
        HA_BINARY_SENSORS,
        HA_BOOLEANS,
        HA_DUMP_LOADS,
        HA_PUMP_SWITCH,
        HA_SENSORS,
        HA_TOKEN,
        HA_URL,
        HA_WATER_VALVE,
        TASMOTA_IPS,
        VUE_SENSORS,
    )
except ImportError:
    # Fallback for development or if local_config.py doesn't exist
    print("WARNING: local_config.py not found! Copy local_config.example.py to local_config.py")
    HA_URL = "http://localhost:8123"  # nosec B310 — local dev fallback
    HA_TOKEN = "your_token_here"
    TASMOTA_IPS = []
    HA_SENSORS = {}
    VUE_SENSORS = {}
    HA_BOOLEANS = {}
    HA_DUMP_LOADS = []
    HA_WATER_VALVE = ""
    HA_PUMP_SWITCH = ""
    HA_BINARY_SENSORS = {}


def _import_local_config(name: str, default=""):
    """Import a variable from local_config with a fallback default."""
    try:
        from local_config import vars as local_vars

        return getattr(local_vars, name, default)
    except (ImportError, AttributeError):
        return default


# Optional laundry controls (may not exist in older local_config.py)
HA_WASHER_POWER = _import_local_config("HA_WASHER_POWER")
HA_DRYER_POWER = _import_local_config("HA_DRYER_POWER")
HA_LAUNDRY_OUTLET = _import_local_config("HA_LAUNDRY_OUTLET")

# =============================================================================
# OPTIONAL FEATURES
# =============================================================================
# Set to False to disable features manually, or leave True for auto-detection.
# Features auto-disable if HA_TOKEN is not configured.
#
# When disabled:
#   - Console output omits the corresponding sections
#   - Web UI hides the corresponding cards
#   - No HA API calls are made for disabled features

ENABLE_EV = True  # EV charging monitoring (car SoC, VUE charger power)
ENABLE_WATER = True  # Water level, pump and valve control
ENABLE_HA_LOADS = True  # Home Assistant loads monitoring (Vue sensors)
ENABLE_DISHWASHER = True  # Dishwasher duration monitoring
ENABLE_WASHER = True  # Washer remaining time monitoring
ENABLE_DRYER = True  # Dryer remaining time monitoring
ENABLE_HA = True  # Home Assistant integration entirely

# Auto-disable all HA features if no valid token configured
if HA_TOKEN in ("", "your_token_here", None):
    ENABLE_HA = False
    ENABLE_EV = False
    ENABLE_WATER = False
    ENABLE_HA_LOADS = False
    ENABLE_DISHWASHER = False
    ENABLE_WASHER = False
    ENABLE_DRYER = False
    print("INFO: Home Assistant disabled (no valid HA_TOKEN in local_config.py)")

# =============================================================================
# MQTT BRIDGE (for remote web dashboard)
# =============================================================================
# MQTT broker address (for remote dashboard connection)
# Set to empty string to disable MQTT bridge
MQTT_BROKER = "localhost"  # Venus OS has built-in MQTT broker
MQTT_PORT = 1883
MQTT_TOPIC_PREFIX = "inverter"

# When True, inverter/state MQTT payload omits Home Assistant mirror fields (booleans,
# switch shadows, appliance flags). Use this with inverter-dashboard ha_secrets.py + HA_DIRECT_CONTROLS
# so switch state is read from HA in the dashboard instead of duplicated over MQTT.
MQTT_SLIM_STATE = True

MQTT_SLIM_EXCLUDE_KEYS = frozenset(
    {
        "laundry_outlet",
        "home_recliner",
        "home_garage",
        "water_valve",
        "pump_switch",
        "dishwasher_running",
        "dishwasher_duration",
        "washer_time",
        "dryer_time",
        "washer_power",
        "dryer_power",
    }
)

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


# Lazy: first access triggers detection (cached for subsequent calls)
PORTAL_ID = _detect_portal_id()

# Power limits for outlet protection (Watts)
POWER_LIMIT_MAX = 2250  # Maximum feed-in (positive = charging battery)
POWER_LIMIT_MIN = -2300  # Maximum export (negative = discharging to grid)

# Control loop timing
LOOP_INTERVAL = 0.33  # seconds (3 times per second)
HA_POLL_INTERVAL = 1.5  # seconds for Home Assistant polling
NO_FEED_SLEEP_INTERVAL = 1.0  # seconds to sleep in no_feed mode (slows loop to ~1 Hz)

# Grid zero targeting - Stability tuning for VM-3P75CT or similar fast CT meters
GRID_ZERO_DEADBAND_LOW = -50  # Watts - lower bound (slight export OK)
GRID_ZERO_DEADBAND_HIGH = 80  # Watts - upper bound (slight import OK)
DAMPING_FACTOR = 0.7  # Damping for import correction (0.0-1.0)
EMA_ALPHA = 0.3  # Exponential Moving Average smoothing (0.1=smooth, 0.5=responsive)
SETPOINT_DELTA_LIMIT = 2000  # Maximum change in setpoint per cycle (Watts)

# Aggressive Grid Smoothing with Home Load (Vue via HA cloud)
# Uses "home_total" (total house consumption from Vue) + known production
# to derive a stable grid estimate. Blends with instantaneous CT meter.
ENABLE_GRID_SMOOTHING_WITH_HOME = False  # Enable derived grid blending
GRID_SMOOTHING_HOME_WEIGHT = 0.7  # Weight for derived grid (0.0-1.0, higher = more stable)
GRID_SMOOTHING_DERIVED_ALPHA = 0.1  # EMA alpha for derived grid (slower = smoother)

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

# HA_URL, HA_TOKEN, HA_SENSORS, VUE_SENSORS, HA_BOOLEANS,
# HA_DUMP_LOADS, HA_WATER_VALVE, HA_BINARY_SENSORS
# are all imported from local_config.py

HA_TIMEOUT = 2.0  # seconds

# Timezone for console output
TIMEZONE = "America/Los_Angeles"

# =============================================================================
# TASMOTA PV INVERTERS (now via D-Bus)
# =============================================================================

# D-Bus service names for Tasmota PV inverters
TASMOTA_DBUS_SERVICES = [
    "com.victronenergy.pvinverter.tasmota_120",
    "com.victronenergy.pvinverter.tasmota_121",
]

# Fallback HTTP polling if D-Bus not available
# TASMOTA_IPS imported from local_config.py

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
        _check_type("PORTAL_ID", PORTAL_ID, str),
        _check_type("TASMOTA_IPS", TASMOTA_IPS, (list, tuple)),
        _check_type("HA_SENSORS", HA_SENSORS, dict),
        _check_type("VUE_SENSORS", VUE_SENSORS, dict),
        _check_type("HA_BOOLEANS", HA_BOOLEANS, dict),
        _check_type("HA_DUMP_LOADS", HA_DUMP_LOADS, (list, tuple)),
        _check_type("LOOP_INTERVAL", LOOP_INTERVAL, (int, float)),
        _check_type("POWER_LIMIT_MAX", POWER_LIMIT_MAX, (int, float)),
        _check_type("POWER_LIMIT_MIN", POWER_LIMIT_MIN, (int, float)),
        _check_type("DAMPING_FACTOR", DAMPING_FACTOR, (int, float)),
        _check_type("EMA_ALPHA", EMA_ALPHA, (int, float)),
        _check_range("DAMPING_FACTOR", DAMPING_FACTOR, 0.0, 1.0),
        _check_range("EMA_ALPHA", EMA_ALPHA, 0.0, 1.0),
    ]

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
    "header_toggles": [
        {
            "id": "only_charging",
            "label": "ONLY CHARGING",
            "entity": "input_boolean.only_charging",
        },
        {"id": "no_feed", "label": "NO FEED", "entity": "input_boolean.no_feed"},
        {
            "id": "house_support",
            "label": "HOUSE SUPPORT",
            "entity": "input_boolean.house_support",
        },
        {
            "id": "charge_battery",
            "label": "CHARGE BATTERY",
            "entity": "input_boolean.charge_battery",
        },
        {
            "id": "do_not_supply_charger",
            "label": "DO NOT SUPPLY EV",
            "entity": "input_boolean.do_not_supply_charger",
        },
        {
            "id": "set_limit_to_ev_charger",
            "label": "LIMIT TO EV",
            "entity": "input_boolean.set_limit_to_ev_charger",
        },
        {
            "id": "minimize_charging",
            "label": "MINIMIZE CHARGING",
            "entity": "input_boolean.minimize_charging",
        },
    ],
    "home_buttons": [
        {
            "id": "recliner",
            "label": "RECLINER",
            "entity": "switch.recliner_recliner",
            "state_key": "home_recliner",
        },
        {
            "id": "garage",
            "label": "GARAGE",
            "entity": "switch.garage_opener_l",
            "state_key": "home_garage",
        },
        {
            "id": "laundry",
            "label": "LAUNDRY",
            "entity": "switch.laundry_zigbee_switch",
            "state_key": "laundry_outlet",
        },
    ],
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
        "valve_entity": "switch.shutoff_valve",
        "pump_entity": "switch.pump_switch",
    },
    "ev": {
        "charging_sensor": "ev_charging_power",
        "power_sensor": "ev_charger",
        "soc_sensor": "car_soc",
    },
}


def get_ui_config() -> dict:
    """Get UI configuration"""
    return UI_CONFIG

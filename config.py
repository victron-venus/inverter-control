#!/usr/bin/env python3
"""
Inverter Control Configuration
All configurable parameters in one place
"""

# =============================================================================
# SECRETS (imported from secrets.py - not tracked by git)
# =============================================================================
try:
    from secrets import (
        HA_URL, HA_TOKEN, PORTAL_ID, TASMOTA_IPS,
        HA_SENSORS, VUE_SENSORS, HA_BOOLEANS,
        HA_DUMP_LOADS, HA_WATER_VALVE, HA_PUMP_SWITCH, HA_BINARY_SENSORS,
        HA_WASHER_POWER, HA_DRYER_POWER
    )
except ImportError:
    # Fallback for development or if secrets.py doesn't exist
    print("WARNING: secrets.py not found! Copy secrets.example.py to secrets.py")
    HA_URL = "http://localhost:8123"
    HA_TOKEN = "your_token_here"
    PORTAL_ID = "your_portal_id"
    TASMOTA_IPS = []
    HA_SENSORS = {}
    VUE_SENSORS = {}
    HA_BOOLEANS = {}
    HA_DUMP_LOADS = []
    HA_WATER_VALVE = ""
    HA_PUMP_SWITCH = ""
    HA_BINARY_SENSORS = {}
    HA_WASHER_POWER = ""
    HA_DRYER_POWER = ""

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

ENABLE_EV = True           # EV charging monitoring (car SoC, VUE charger power)
ENABLE_WATER = True        # Water level, pump and valve control
ENABLE_HA_LOADS = True     # Home Assistant loads monitoring (Vue sensors)
ENABLE_DISHWASHER = True   # Dishwasher duration monitoring
ENABLE_WASHER = True       # Washer remaining time monitoring
ENABLE_DRYER = True        # Dryer remaining time monitoring
ENABLE_HA = True           # Home Assistant integration entirely

# Auto-disable all HA features if no valid token configured
if HA_TOKEN in ("", "your_token_here", None):
    ENABLE_HA = False
    ENABLE_EV = False
    ENABLE_WATER = False
    ENABLE_HA_LOADS = False
    ENABLE_DISHWASHER = False
    ENABLE_WASHER = False
    ENABLE_DRYER = False
    print("INFO: Home Assistant disabled (no valid HA_TOKEN in secrets.py)")

# =============================================================================
# RUNTIME MODE
# =============================================================================

# Dry-run mode: if True, don't send commands to Victron (safe for testing)
# Can be toggled via web interface at runtime
DRY_RUN = False  # Live mode - sending commands to Victron

# =============================================================================
# VICTRON SYSTEM
# =============================================================================

# PORTAL_ID imported from secrets.py

# Power limits for outlet protection (Watts)
POWER_LIMIT_MAX = 2250      # Maximum feed-in (positive = charging battery)
POWER_LIMIT_MIN = -2300     # Maximum export (negative = discharging to grid)

# Control loop timing
LOOP_INTERVAL = 0.33        # seconds (3 times per second)
HA_POLL_INTERVAL = 1.5      # seconds for Home Assistant polling

# Grid zero targeting
GRID_ZERO_DEADBAND = 20     # Watts - don't adjust if grid within this range
GRID_CORRECTION_SMALL = 5   # Watts - small correction step
DAMPING_FACTOR = 3          # Damping for large corrections

# Solar output offset - reduce output by this amount to avoid grid export
# Used in only_charging, do_not_supply_charger, and other solar-limited modes
SOLAR_OUTPUT_OFFSET = 60    # Watts

# =============================================================================
# HOME ASSISTANT
# =============================================================================

# HA_URL, HA_TOKEN, HA_SENSORS, VUE_SENSORS, HA_BOOLEANS,
# HA_DUMP_LOADS, HA_WATER_VALVE, HA_BINARY_SENSORS
# are all imported from secrets.py

HA_TIMEOUT = 2.0            # seconds

# Timezone for console output
TIMEZONE = 'America/Los_Angeles'

# =============================================================================
# TASMOTA PV INVERTERS (now via D-Bus)
# =============================================================================

# D-Bus service names for Tasmota PV inverters
TASMOTA_DBUS_SERVICES = [
    'com.victronenergy.pvinverter.tasmota_120',
    'com.victronenergy.pvinverter.tasmota_121',
]

# Fallback HTTP polling if D-Bus not available
# TASMOTA_IPS imported from secrets.py

# =============================================================================
# WEB SERVER
# =============================================================================

WEB_PORT = 8080
WEB_HOST = '0.0.0.0'

# History for graphs (seconds)
HISTORY_DURATION = 3600     # 1 hour of history
HISTORY_INTERVAL = 2        # Store point every 2 seconds

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

# SSL Configuration (for HTTPS)
SSL_ENABLED = True
SSL_CERT = "/data/inverter_control/inverter-control.crt"
SSL_KEY = "/data/inverter_control/inverter-control.key"


class Colors:
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN = '\033[36m'
    WHITE = '\033[37m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

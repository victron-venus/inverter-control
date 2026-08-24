#!/usr/bin/env python3
"""
Site Configuration Example

Copy this file to local_config.py and fill in your values.
local_config.py is NOT tracked by git.
"""

# =============================================================================
# HOME ASSISTANT CONNECTION
# =============================================================================

HA_URL = "http://YOUR_HA_IP:8123"  # nosec B310 — local HA instance
HA_TOKEN = "your_long_lived_access_token_here"

# =============================================================================
# VICTRON
# =============================================================================

# PORTAL_ID (VRM Portal ID) is auto-detected at runtime from /sbin/get-unique-id
# (fallback: eth0 MAC address). No need to configure it here.

# =============================================================================
# TIME-OF-USE EXPENSIVE GRID WINDOW (local hours)
# =============================================================================
# Solar-forecast pre-charge is suppressed while the wall-clock hour (from the
# GX /Settings/System/TimeZone setting) is inside [START, END).
# Set both to -1 to disable. Windows may wrap midnight (e.g. 22 -> 6).
# NOTE: on Cerbo the GUI-editable Settings entries under /Settings/InverterControl
# (created automatically at startup) take precedence over these values; these
# serve as bootstrap defaults and for non-Cerbo environments.

TOU_EXPENSIVE_START_HOUR = 15  # 3 PM
TOU_EXPENSIVE_END_HOUR = 24  # midnight

# =============================================================================
# TASMOTA DEVICES
# =============================================================================

TASMOTA_IPS = ["192.168.x.x", "192.168.x.x"]

# =============================================================================
# HOME ASSISTANT SENSORS
# =============================================================================
# Note: "home_total" removed - now read from Vue D-Bus (com.victronenergy.acload.Total)
# via HomeAssistantClient.get_vue_sensor("total")
# Note: produced_today, battery_in/out_today, tasmota_*_daily, mppt_*_daily,
# pv_total_daily, compensation_voltage removed - now read from D-Bus

HA_SENSORS = {
    "net_usage": "sensor.your_net_usage",
    "car_soc": "sensor.your_car_soc",
    "ev_charging_power": "sensor.your_ev_charging_power",
    "water_level": "sensor.your_water_level",
    "laundry_power": "sensor.your_laundry_power",
    "washer_time": "sensor.your_washer_time",
    "dryer_time": "sensor.your_dryer_time",
    "dishwasher_duration": "sensor.your_dishwasher_duration",
    "produced_dollars": "sensor.your_produced_dollars",
    "battery_in_yesterday": "sensor.your_battery_in_yesterday",
    "battery_out_yesterday": "sensor.your_battery_out_yesterday",
    "grid_kwh_today": "sensor.your_grid_kwh_today",
}

# =============================================================================
# VUE POWER SENSORS (Deprecated - Auto-discovered via D-Bus acload services)
# =============================================================================
# Emporia Vue channels registered by dbus-emporia-vue (com.victronenergy.acload.*)
# are automatically discovered on Venus OS D-Bus. No manual configuration needed.
# Optional override mapping (key -> custom_name) if custom key names are desired:

VUE_SENSORS = {}

# =============================================================================
# HOME ASSISTANT CONTROL ENTITIES
# =============================================================================

HA_BOOLEANS = {
    "only_charging": "input_boolean.only_charging",
    "no_feed": "input_boolean.no_feed",
    "house_support": "input_boolean.house_support",
    "charge_battery": "input_boolean.charge_battery",
    "do_not_supply_charger": "input_boolean.do_not_supply_charger",
    "set_limit_to_ev_charger": "input_boolean.set_limit_to_ev_charger",
    "minimize_charging": "input_boolean.minimize_charging",
}

HA_DUMP_LOADS = [
    "switch.your_dump_load_1",
    "switch.your_dump_load_2",
]

# =============================================================================
# WATER SYSTEM (dbus-pump D-Bus services on the GX - no Home Assistant)
# =============================================================================
# Must match DEVICE_INSTANCE_TANK / PUMP_STARTSTOP_INSTANCE /
# VALVE_STARTSTOP_INSTANCE in dbus-pump's local_config.py.
WATER_TANK_INSTANCE = 21
WATER_PUMP_INSTANCE = 1
WATER_VALVE_INSTANCE = 2

# Laundry appliance controls
HA_WASHER_POWER = "switch.washer_power"
HA_WASHER_PAUSE = "button.washer_pause"
HA_DRYER_POWER = "switch.dryer_power"
HA_DRYER_PAUSE = "button.dryer_pause"
HA_LAUNDRY_OUTLET = "switch.laundry_zigbee_switch"  # Smart outlet for washer/dryer standby power

HA_BINARY_SENSORS = {
    "dishwasher_running": "binary_sensor.your_dishwasher",
}

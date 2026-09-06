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
# HOME ASSISTANT SENSORS
# =============================================================================
# Note: setpoint path uses Cerbo grid/CT + optional HA sensors.
# com.victronenergy.acload.* D-Bus channels are not consumed.
# Note: produced_today, battery_in/out_today, mppt_*_daily,
# pv_total_daily, compensation_voltage removed - now read from D-Bus

HA_SENSORS = {
    "net_usage": "sensor.your_net_usage",
    # Note: car_soc, ev_charging_power, ev_charger moved to D-Bus (see EV_INSTANCE below)
    # Note: water_level, produced_dollars, battery_in/out_yesterday, grid_kwh_today,
    # washer_time, dryer_time, dishwasher_duration, laundry_power are no longer
    # polled from Home Assistant. Water comes from dbus-pump D-Bus, EV from
    # dbus-evcharger/dbus-ev D-Bus, appliance timers and energy totals are
    # surfaced from local D-Bus or the in-process inverter-control state.
}

# =============================================================================
# HOME-LOAD SENSORS (legacy VUE_SENSORS mapping; optional HA home_total)
# =============================================================================
# Used by grid smoothing / console home_total when populated via HA entities.
# Empty by default. Not related to D-Bus acload services.

VUE_SENSORS = {}

# =============================================================================
# HOME ASSISTANT DUMP LOADS
# =============================================================================

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

# =============================================================================
# EV CHARGER / VEHICLE (dbus-evcharger + dbus-ev D-Bus services on the GX)
# =============================================================================
# dbus-evcharger exposes com.victronenergy.evcharger.<N> (wallbox).
# dbus-ev exposes com.victronenergy.ev.<suffix> (vehicle, has /Soc /VIN).
# Both services are autodetected; set overrides only if auto-detection fails.
# Default: EV_INSTANCE = 22 (vehicle), EVCHARGER_INSTANCE = 40 (wallbox)
EV_INSTANCE = 22
EVCHARGER_INSTANCE = 40

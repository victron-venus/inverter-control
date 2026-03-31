#!/usr/bin/env python3
"""
Secrets Configuration Example

Copy this file to secrets.py and fill in your values.
secrets.py is NOT tracked by git.
"""

# =============================================================================
# HOME ASSISTANT CONNECTION
# =============================================================================

HA_URL = "http://YOUR_HA_IP:8123"
HA_TOKEN = "your_long_lived_access_token_here"

# =============================================================================
# VICTRON
# =============================================================================

PORTAL_ID = "your_portal_id"

# =============================================================================
# TASMOTA DEVICES
# =============================================================================

TASMOTA_IPS = ['192.168.x.x', '192.168.x.x']

# =============================================================================
# HOME ASSISTANT SENSORS
# =============================================================================

HA_SENSORS = {
    'home_total': 'sensor.your_home_total_power',
    'net_usage': 'sensor.your_net_usage',
    'car_soc': 'sensor.your_car_soc',
    'ev_charging_power': 'sensor.your_ev_charging_power',
    'water_level': 'sensor.your_water_level',
    'laundry_power': 'sensor.your_laundry_power',
    'washer_time': 'sensor.your_washer_time',
    'dryer_time': 'sensor.your_dryer_time',
    'dishwasher_duration': 'sensor.your_dishwasher_duration',
    'produced_today': 'sensor.your_produced_today',
    'produced_dollars': 'sensor.your_produced_dollars',
    'battery_in_today': 'sensor.your_battery_in_today',
    'battery_out_today': 'sensor.your_battery_out_today',
    'battery_in_yesterday': 'sensor.your_battery_in_yesterday',
    'battery_out_yesterday': 'sensor.your_battery_out_yesterday',
    'grid_kwh_today': 'sensor.your_grid_kwh_today',
    'corrected_soc': 'sensor.your_corrected_soc',
    'compensation_voltage': 'sensor.your_compensation_voltage',
    'tasmota_1_daily': 'sensor.your_tasmota_1_daily',
    'tasmota_2_daily': 'sensor.your_tasmota_2_daily',
    'pv_total_daily': 'sensor.your_pv_total_daily',
    'mppt_1_daily': 'sensor.your_mppt_1_daily',
    'mppt_2_daily': 'sensor.your_mppt_2_daily',
    'mppt_3_daily': 'sensor.your_mppt_3_daily',
}

# =============================================================================
# VUE POWER SENSORS
# =============================================================================

VUE_SENSORS = {
    'garage': 'sensor.your_garage',
    'ev_charger': 'sensor.your_ev_charger',
    'fridge': 'sensor.your_fridge',
    # Add more circuits as needed
}

# =============================================================================
# HOME ASSISTANT CONTROL ENTITIES
# =============================================================================

HA_BOOLEANS = {
    'only_charging': 'input_boolean.only_charging',
    'no_feed': 'input_boolean.no_feed',
    'house_support': 'input_boolean.house_support',
    'charge_battery': 'input_boolean.charge_battery',
    'do_not_supply_charger': 'input_boolean.do_not_supply_charger',
    'set_limit_to_ev_charger': 'input_boolean.set_limit_to_ev_charger',
    'minimize_charging': 'input_boolean.minimize_charging',
}

HA_DUMP_LOADS = [
    'switch.your_dump_load_1',
    'switch.your_dump_load_2',
]

HA_WATER_VALVE = 'switch.your_water_valve'
HA_PUMP_SWITCH = 'switch.your_pump_switch'

# Laundry appliance controls
HA_WASHER_POWER = 'switch.washer_power'
HA_WASHER_PAUSE = 'button.washer_pause'
HA_DRYER_POWER = 'switch.dryer_power'
HA_DRYER_PAUSE = 'button.dryer_pause'

HA_BINARY_SENSORS = {
    'dishwasher_running': 'binary_sensor.your_dishwasher',
}

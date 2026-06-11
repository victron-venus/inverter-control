"""
UI Configuration for Inverter Control
Default configuration for dashboard UI elements
"""

import logging

logger = logging.getLogger("inverter-control")


def get_ui_config() -> dict:
    """Get UI configuration"""
    return {
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

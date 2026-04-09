"""
UI Configuration loader for Inverter Control
Loads ui_config.yaml and provides it to the web interface
"""

import os
import logging

logger = logging.getLogger('inverter-control')

# Try to import PyYAML
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    logger.warning("PyYAML not available, using default UI config")


def load_ui_config() -> dict:
    """Load UI configuration from yaml file"""
    config_path = os.path.join(os.path.dirname(__file__), 'ui_config.yaml')
    
    if not YAML_AVAILABLE:
        return get_default_config()
    
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
            logger.info(f"Loaded UI config from {config_path}")
            return config
    except FileNotFoundError:
        logger.warning(f"UI config not found at {config_path}, using defaults")
        return get_default_config()
    except Exception as e:
        logger.error(f"Failed to load UI config: {e}")
        return get_default_config()


def get_default_config() -> dict:
    """Return default UI configuration"""
    return {
        'header_toggles': [
            {'id': 'only_charging', 'label': 'ONLY CHARGING', 'entity': 'input_boolean.only_charging'},
            {'id': 'no_feed', 'label': 'NO FEED', 'entity': 'input_boolean.no_feed'},
            {'id': 'house_support', 'label': 'HOUSE SUPPORT', 'entity': 'input_boolean.house_support'},
            {'id': 'charge_battery', 'label': 'CHARGE BATTERY', 'entity': 'input_boolean.charge_battery'},
            {'id': 'do_not_supply_charger', 'label': 'DO NOT SUPPLY EV', 'entity': 'input_boolean.do_not_supply_charger'},
            {'id': 'set_limit_to_ev_charger', 'label': 'LIMIT TO EV', 'entity': 'input_boolean.set_limit_to_ev_charger'},
            {'id': 'minimize_charging', 'label': 'MINIMIZE CHARGING', 'entity': 'input_boolean.minimize_charging'},
        ],
        'home_buttons': [
            {'id': 'recliner', 'label': 'RECLINER', 'entity': 'switch.recliner_recliner', 'type': 'toggle', 'state_key': 'home_recliner'},
            {'id': 'garage', 'label': 'GARAGE', 'entity': 'switch.garage_opener_l', 'type': 'toggle', 'state_key': 'home_garage'},
            {'id': 'laundry', 'label': 'LAUNDRY', 'entity': 'switch.laundry_zigbee_switch', 'type': 'toggle', 'state_key': 'laundry_outlet'},
        ],
        'batteries': [
            {'id': 'virtual', 'name': 'Virtual Battery', 'dbus_service': 'com.victronenergy.battery.virtual_chain', 'show_current': True, 'show_power': True},
            {'id': 'chain1', 'name': 'JBD Chain 1', 'dbus_service': 'com.victronenergy.battery.dbus-mqtt-chain1', 'show_current': True, 'show_power': True},
            {'id': 'chain2', 'name': 'JBD Chain 2', 'dbus_service': 'com.victronenergy.battery.dbus-mqtt-chain2', 'show_current': True, 'show_power': True},
        ],
        'solar_sources': {
            'mppt_names': {0: 'MPPT-290', 1: 'MPPT-291', 2: 'MPPT-292'},
            'pv_inverters': [
                {'id': 'pv1', 'name': 'PV Inverter 1', 'index': 0},
                {'id': 'pv2', 'name': 'PV Inverter 2', 'index': 1},
            ]
        },
        'loads': {
            'hidden': ['solar_shed'],
            'min_watts': 10,
        },
        'water': {
            'valve_entity': 'switch.778_40th_ave_sf_shutoff_valve',
            'pump_entity': 'switch.pump_switch',
            'level_sensor': 'water_level',
        },
        'ev': {
            'charging_sensor': 'ev_charging_power',
            'power_sensor': 'ev_charger',
            'soc_sensor': 'car_soc',
        },
    }


# Global config instance
_ui_config = None


def get_ui_config() -> dict:
    """Get UI configuration (cached)"""
    global _ui_config
    if _ui_config is None:
        _ui_config = load_ui_config()
    return _ui_config


def reload_ui_config() -> dict:
    """Force reload UI configuration"""
    global _ui_config
    _ui_config = load_ui_config()
    return _ui_config

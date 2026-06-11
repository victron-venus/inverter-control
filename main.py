#!/usr/bin/env python3
"""
Inverter Control - Main Entry Point
Grid-zero feed-in control for Victron system with split-phase compensation
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time
import argparse
import signal
import logging
import traceback
import atexit
import gc
from typing import Dict, Any, Optional

from config import (
    POWER_LIMIT_MAX, POWER_LIMIT_MIN, LOOP_INTERVAL,
    GRID_ZERO_DEADBAND_LOW, GRID_ZERO_DEADBAND_HIGH, DAMPING_FACTOR, EMA_ALPHA,
    SOLAR_OUTPUT_OFFSET, INVERTER_EFFICIENCY, SETPOINT_DELTA_LIMIT,
    Colors as C,
    DRY_RUN,
    ENABLE_EV, ENABLE_WATER, ENABLE_HA, ENABLE_HA_LOADS,
    MQTT_SLIM_STATE, MQTT_SLIM_EXCLUDE_KEYS,
    CREEP_RATE, CREEP_MAX,
    EXPORT_DAMPING,
)
from victron import get_victron
from homeassistant import get_ha
from console_server import start_server as start_console_server, stop_server as stop_console_server, broadcast_line
from logic import SetpointCalculator, SystemState
from console_ui import ConsoleUI

try:
    from mqtt_bridge import get_mqtt_bridge, MQTT_AVAILABLE
except ImportError:
    MQTT_AVAILABLE = False
    def get_mqtt_bridge(*a, **kw):
        return None

# =============================================================================
# LOGGING SETUP - All errors go to file
# =============================================================================
LOG_FILE = "/var/log/inverter-control.log"

logger = logging.getLogger('inverter-control')
logger.setLevel(logging.DEBUG)

try:
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(fh)
except Exception as e:
    print(f"Warning: Could not create log file: {e}", file=sys.stderr)


def log_exception(msg: str):
    """Log exception with full traceback"""
    logger.error(f"{msg}\n{traceback.format_exc()}")


def get_version() -> str:
    """Read version from version file"""
    try:
        version_file = os.path.join(os.path.dirname(__file__), 'version')
        with open(version_file, 'r') as f:
            return f.read().strip()
    except Exception:
        return 'unknown'


VERSION = get_version()


class TimeoutError(Exception):
    """Raised when a watchdog timeout occurs"""
    pass


# =============================================================================
# INVERTER CONTROLLER
# =============================================================================

class InverterController:
    """
    Main controller for grid-zero feed-in management.
    Coordinates I/O (D-Bus, HA) and delegates logic to SetpointCalculator.
    """
    
    def __init__(self, dry_run: Optional[bool] = None):
        self.dry_run = dry_run if dry_run is not None else DRY_RUN
        self.victron = get_victron()
        self.ha = get_ha()
        
        # Load UI configuration
        from ui_config import get_ui_config
        self.ui_config = get_ui_config()
        
        # Initialize Logic and UI components
        config_dict = {
            'EMA_ALPHA': EMA_ALPHA,
            'POWER_LIMIT_MIN': POWER_LIMIT_MIN,
            'POWER_LIMIT_MAX': POWER_LIMIT_MAX,
            'SETPOINT_DELTA_LIMIT': SETPOINT_DELTA_LIMIT,
            'DAMPING_FACTOR': DAMPING_FACTOR,
            'GRID_ZERO_DEADBAND_LOW': GRID_ZERO_DEADBAND_LOW,
            'GRID_ZERO_DEADBAND_HIGH': GRID_ZERO_DEADBAND_HIGH,
            'INVERTER_EFFICIENCY': INVERTER_EFFICIENCY,
            'SOLAR_OUTPUT_OFFSET': SOLAR_OUTPUT_OFFSET,
            'CREEP_RATE': CREEP_RATE,
            'CREEP_MAX': CREEP_MAX,
            'EXPORT_DAMPING': EXPORT_DAMPING,
        }
        self.calculator = SetpointCalculator(config_dict)
        self.console = ConsoleUI(self.ha, self.victron)
        
        # State
        self.start_time = time.time()
        self.current_setpoint = 0
        self.previous_setpoint = 0
        self.manual_setpoint: Optional[int] = None
        self.delay = 0  # Delay counter for load switching
        self.filtered_gt: Optional[float] = None
        
        self.loop_count = 0
        self.state: Dict[str, Any] = {}
        
        # Cached D-Bus data
        self._cached_mppt_data = {}
        self._cached_tasmota_powers = []
        self._cached_battery_socs = []
        self._cached_inv_state = ""
        
        # Dynamic settings (overridable)
        self.power_limit_min = POWER_LIMIT_MIN
        self.power_limit_max = POWER_LIMIT_MAX
        self.loop_interval = LOOP_INTERVAL

    def set_loop_interval(self, interval: float) -> float:
        self.loop_interval = max(0.1, min(5.0, interval))
        logger.info(f"Loop interval changed to {self.loop_interval}s")
        return self.loop_interval
    
    def set_power_limits(self, min_val: int, max_val: int) -> Dict[str, int]:
        self.power_limit_min = max(min_val, -3000)
        self.power_limit_max = min(max_val, 3000)
        # Update calculator limits
        self.calculator.power_limit_min = self.power_limit_min
        self.calculator.power_limit_max = self.power_limit_max
        logger.info(f"Power limits changed to [{self.power_limit_min}, {self.power_limit_max}]")
        return {'min': self.power_limit_min, 'max': self.power_limit_max}
    
    def toggle_dry_run(self) -> bool:
        self.dry_run = not self.dry_run
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(f"Mode changed to {mode}")
        return self.dry_run
    
    def toggle_ess_mode(self) -> Dict[str, Any]:
        current = self.victron.get_ess_mode()
        new_external = not current['is_external']
        if self.victron.set_ess_mode(external=new_external):
            new_mode = self.victron.get_ess_mode()
            logger.info(f"ESS Mode changed to {new_mode['mode_name']}")
            return new_mode
        return current
    
    def get_state(self) -> Dict[str, Any]:
        return self.state

    def set_manual_setpoint(self, value: int) -> bool:
        self.manual_setpoint = max(self.power_limit_min, min(self.power_limit_max, value))
        return True
    
    def calculate_setpoint(self, sys_data: Dict[str, Any]) -> tuple[int, str]:
        """Orchestrate state collection and delegate calculation to logic.py"""
        
        # Prepare SystemState snapshot
        mppt_data = self.victron.get_mppt_data()
        mppt_total = sum(m['w'] for m in mppt_data.values())
        tasmota_powers = self.victron.get_tasmota_pv_power()
        tasmota_total = sum(tasmota_powers)
        
        state = SystemState(
            g1=sys_data['g1'], g2=sys_data['g2'], gt=sys_data['gt'],
            t1=sys_data['t1'], t2=sys_data['t2'], tt=sys_data['tt'],
            inv_power=self.victron.get_inverter_power(),
            mppt_total=mppt_total,
            tasmota_total=tasmota_total,
            pv_total=mppt_total + tasmota_total,
            ev_power=self.ha.get_vue_sensor('ev_charger', 0),
            garage_power=self.ha.get_vue_sensor('garage', 0),
            only_charging=self.ha.get_boolean('only_charging'),
            no_feed=self.ha.get_boolean('no_feed'),
            house_support=self.ha.get_boolean('house_support'),
            charge_battery=self.ha.get_boolean('charge_battery'),
            do_not_supply_charger=self.ha.get_boolean('do_not_supply_charger'),
            limit_to_ev=self.ha.get_boolean('set_limit_to_ev_charger'),
            previous_setpoint=self.previous_setpoint,
            filtered_gt=self.filtered_gt
        )
        
        # Perform calculation
        result = self.calculator.calculate(state)
        
        # Update persistence
        self.filtered_gt = result.filtered_gt
        
        return result.setpoint, result.flags
    
    def handle_minimize_charging(self, sys_data: Dict[str, Any]):
        try:
            if self.delay > 0:
                self.delay -= 1
                return
            if not self.ha.get_boolean('minimize_charging'):
                return
            inverter_state, _ = self.victron.get_inverter_state()
            if inverter_state == 0:
                return
            net_usage = self.ha.get_sensor('net_usage', 0)
            bp = sys_data.get('bp', 0)
            if 0 < net_usage < 200 and bp > 750:
                changed = self.ha.control_dump_loads(turn_on=True)
                if changed > 0:
                    self.delay = 6
                    print(f" [MC+{changed}] ", end='')
            elif bp < -650 or net_usage > 650:
                changed = self.ha.control_dump_loads(turn_on=False)
                if changed > 0:
                    self.delay = 6
                    print(f" [MC-{changed}] ", end='')
        except Exception as e:
            logger.warning(f"minimize_charging error: {e}")

    def update_state(self, sys_data: Dict[str, Any], setpoint: int):
        self._cached_mppt_data = self.victron.get_mppt_data()
        self._cached_tasmota_powers = self.victron.get_tasmota_pv_power()
        self._cached_battery_socs = self.victron.get_battery_chain_socs()
        _, self._cached_inv_state = self.victron.get_inverter_state()
        
        # Inject cached data into sys_data for console UI use
        sys_data['mppt_data'] = self._cached_mppt_data
        sys_data['tasmota_powers'] = self._cached_tasmota_powers
        sys_data['battery_socs'] = self._cached_battery_socs
        
        # Full state for web UI
        self.state = {
            **sys_data,
            'setpoint': setpoint,
            'filtered_gt': self.filtered_gt,
            'dry_run': self.dry_run,
            'mppt_total': sum(m['w'] for m in self._cached_mppt_data.values()),
            'tasmota_total': sum(self._cached_tasmota_powers),
            'solar_total': sum(m['w'] for m in self._cached_mppt_data.values()) + sum(self._cached_tasmota_powers),
            'mppt_data': self._cached_mppt_data,
            'inverter_state': self._cached_inv_state,
            'battery_socs': self._cached_battery_socs,
            'batteries': self.victron.get_all_batteries(),
            'mppt_chargers': self.victron.get_mppt_chargers(),
            'ev_power': self.ha.get_vue_sensor('ev_charger', 0) if ENABLE_EV else 0,
            'car_soc': self.ha.get_sensor('car_soc', 0) if ENABLE_EV else 0,
            'water_level': self.ha.get_sensor('water_level', 0) if ENABLE_WATER else 0,
            'water_valve': self.ha.water_valve_on if ENABLE_WATER else False,
            'pump_switch': self.ha.pump_switch_on if ENABLE_WATER else False,
            'booleans': self.ha.get_all_booleans() if ENABLE_HA else {},
            'loads': self.ha.get_all_vue_sensors() if ENABLE_HA_LOADS else {},
            'laundry_outlet': self.ha.laundry_outlet_on if ENABLE_HA else False,
            'home_recliner': self.ha.home_recliner_on if ENABLE_HA else False,
            'home_garage': self.ha.home_garage_on if ENABLE_HA else False,
            'ha_connected': self.ha.connected if ENABLE_HA else False,
            'ha_uptime': self.ha.uptime if ENABLE_HA else 0,
            'ess_mode': self.victron.get_ess_mode(),
            'daily_stats': {
                'produced_today': self.ha.get_sensor('produced_today', 0),
                'grid_kwh': self.ha.get_sensor('grid_kwh_today', 0),
                'battery_in': self.ha.get_sensor('battery_in_today', 0),
                'battery_out': self.ha.get_sensor('battery_out_today', 0),
                'tasmota_daily': [self.ha.get_sensor('tasmota_1_daily', 0), self.ha.get_sensor('tasmota_2_daily', 0)],
                'mppt_daily': [self.ha.get_sensor('mppt_1_daily', 0), self.ha.get_sensor('mppt_2_daily', 0), self.ha.get_sensor('mppt_3_daily', 0)],
            } if ENABLE_HA else {},
            'limits': {'min': self.power_limit_min, 'max': self.power_limit_max},
            'loop_interval': self.loop_interval,
            'version': VERSION,
            'ui_config': self.ui_config,
        }

    def get_state_for_mqtt(self) -> Dict[str, Any]:
        if not MQTT_SLIM_STATE:
            return self.state
        out = dict(self.state)
        for k in MQTT_SLIM_EXCLUDE_KEYS:
            out.pop(k, None)
        return out

    def run_cycle(self) -> bool:
        def watchdog_handler(signum, frame):
            raise TimeoutError("Control cycle watchdog timeout")
        old_handler = signal.signal(signal.SIGALRM, watchdog_handler)
        signal.alarm(5)
        try:
            sys_data = self.victron.get_system_data()
            if self.manual_setpoint is not None:
                setpoint = self.manual_setpoint
                flags = "[MANUAL] "
                self.manual_setpoint = None
            else:
                setpoint, flags = self.calculate_setpoint(sys_data)
            
            self.handle_minimize_charging(sys_data)
            if self.dry_run:
                flags = f"{C.MAGENTA}[DRY]{C.RESET}" + flags
            else:
                self.victron.set_grid_setpoint(setpoint)
            
            print(f"\033k{sys_data['gt']}\033\\", end='')
            
            # Inject cached data for console UI
            sys_data['battery_socs'] = self._cached_battery_socs
            sys_data['mppt_data'] = self._cached_mppt_data
            sys_data['tasmota_powers'] = self._cached_tasmota_powers
            
            filtered_display = self.filtered_gt if self.filtered_gt is not None else sys_data['gt']
            line = self.console.format_line(sys_data, setpoint, self.previous_setpoint, flags, filtered_display)
            print(line)
            broadcast_line(line)
            
            self.update_state(sys_data, setpoint)
            self.console.update_terminal_title()
            self.previous_setpoint = setpoint
            
            try:
                if self.ha.get_boolean('no_feed'):
                    time.sleep(2)
            except Exception:
                pass
            return True
        except KeyboardInterrupt:
            return False
        except TimeoutError:
            logger.error("WATCHDOG: Cycle timeout")
            return True
        except Exception as e:
            log_exception(f"Error in control cycle: {e}")
            return True
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    logger.info("=== Inverter Control starting ===")
    
    try:
        _main_inner()
    except Exception as e:
        log_exception(f"FATAL ERROR in main: {e}")
        raise

def _main_inner():
    parser = argparse.ArgumentParser(description='Inverter Control for Victron System')
    parser.add_argument('setpoint', type=int, nargs='?', default=None,
                       help='Manual setpoint (one-shot mode)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Don\'t actually send commands')
    args = parser.parse_args()
    
    print(f"=== Inverter Control {VERSION} ===")
    
    # Determine dry-run mode: CLI overrides config
    dry_run_mode = args.dry_run if args.dry_run else None
    controller = InverterController(dry_run=dry_run_mode)
    
    mode = "DRY-RUN (safe mode)" if controller.dry_run else "LIVE (sending commands)"
    print(f"Mode: {mode}")
    
    # Start MQTT bridge for remote dashboard
    mqtt_bridge = None
    from config import MQTT_BROKER, MQTT_PORT, MQTT_TOPIC_PREFIX
    if MQTT_AVAILABLE and MQTT_BROKER:
        mqtt_bridge = get_mqtt_bridge(broker=MQTT_BROKER, port=MQTT_PORT, prefix=MQTT_TOPIC_PREFIX)
        if mqtt_bridge:
            mqtt_bridge.connect()
            # Register command callbacks
            mqtt_bridge.register_callback('toggle', lambda p: controller.ha.toggle_entity(p.get('entity', '')))
            mqtt_bridge.register_callback('press', lambda p: controller.ha.press_button(p.get('entity', '')))
            mqtt_bridge.register_callback('setpoint', lambda p: controller.set_manual_setpoint(int(p.get('value', 0))))
            mqtt_bridge.register_callback('dry_run', lambda p: controller.toggle_dry_run())
            mqtt_bridge.register_callback('limits', lambda p: controller.set_power_limits(p.get('min', -2300), p.get('max', 2250)))
            mqtt_bridge.register_callback('ess_mode', lambda p: controller.toggle_ess_mode())
            mqtt_bridge.register_callback('loop_interval', lambda p: controller.set_loop_interval(float(p.get('interval', 0.33))))
            print(f"  MQTT bridge: {MQTT_BROKER}:{MQTT_PORT} (topic: {MQTT_TOPIC_PREFIX}/)")
    
    # If manual setpoint provided, run once and exit
    if args.setpoint is not None:
        controller.manual_setpoint = args.setpoint
        controller.run_cycle()
        return
    
    # Start TCP console server for remote monitoring
    start_console_server()
    
    # Main loop
    print("Starting control loop...")
    print("-" * 80)
    
    # Memory management: run gc periodically
    gc_interval = 300  # Every 5 minutes
    last_gc_time = time.time()
    
    try:
        while True:
            result = controller.run_cycle()
            if not result:
                logger.info("run_cycle returned False - exiting main loop")
                break
            
            # Publish state to MQTT for remote dashboard
            if mqtt_bridge and mqtt_bridge.connected:
                mqtt_bridge.publish_state(controller.get_state_for_mqtt())
            
            # Periodic garbage collection (free memory on resource-constrained Venus OS)
            now = time.time()
            if now - last_gc_time > gc_interval:
                last_gc_time = now
                gc.collect()
            
            time.sleep(controller.loop_interval)
    except KeyboardInterrupt:
        logger.info("Shutdown requested (KeyboardInterrupt)")
        print("\nShutting down...")
    finally:
        logger.info("Inverter Control shutting down")
        stop_console_server()
        if mqtt_bridge:
            mqtt_bridge.disconnect()
        controller.ha.stop()


def signal_handler(signum, frame):
    """Log signal and exit"""
    sig_names = {signal.SIGTERM: 'SIGTERM', signal.SIGINT: 'SIGINT', signal.SIGHUP: 'SIGHUP'}
    sig_name = sig_names.get(signum, f'signal {signum}')
    logger.warning(f"Received {sig_name} - shutting down")
    sys.exit(0)

def excepthook(exc_type, exc_value, exc_tb):
    """Log uncaught exceptions"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.error(f"Uncaught exception: {exc_type.__name__}: {exc_value}\n{''.join(traceback.format_tb(exc_tb))}")

def exit_handler():
    """Log on normal exit"""
    logger.info("Process exiting")

if __name__ == "__main__":
    # Install handlers to track exit reasons
    sys.excepthook = excepthook
    atexit.register(exit_handler)
    
    # Install signal handlers to log shutdown reason
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    main()

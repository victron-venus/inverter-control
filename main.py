#!/usr/bin/env python3
"""
Inverter Control - Main Entry Point
Grid-zero feed-in control for Victron system with split-phase compensation
"""

import sys
import os
import time
import argparse
import signal
import logging
import traceback
import atexit
import gc
from datetime import datetime, timezone
from typing import Dict, Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# =============================================================================
# LOGGING SETUP - All errors go to file
# =============================================================================
LOG_FILE = "/var/log/inverter-control.log"

# Create logger
logger = logging.getLogger('inverter-control')
logger.setLevel(logging.DEBUG)

# File handler - INFO level for startup/shutdown, WARNING for issues
try:
    fh = logging.FileHandler(LOG_FILE)
    fh.setLevel(logging.INFO)  # Log INFO+ to file (startup, shutdown, errors)
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

# Version
def get_version() -> str:
    """Read version from version file"""
    try:
        version_file = os.path.join(os.path.dirname(__file__), 'version')
        with open(version_file, 'r') as f:
            return f.read().strip()
    except:
        return 'unknown'

VERSION = get_version()


class TimeoutError(Exception):
    """Raised when a watchdog timeout occurs"""
    pass

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    POWER_LIMIT_MAX, POWER_LIMIT_MIN, LOOP_INTERVAL,
    GRID_ZERO_DEADBAND_LOW, GRID_ZERO_DEADBAND_HIGH, DAMPING_FACTOR, EMA_ALPHA,
    SOLAR_OUTPUT_OFFSET, INVERTER_EFFICIENCY,
    INVERTER_STATES, Colors as C,
    HA_BOOLEANS, DRY_RUN, TIMEZONE,
    ENABLE_EV, ENABLE_WATER, ENABLE_HA_LOADS, ENABLE_HA,
    ENABLE_DISHWASHER, ENABLE_WASHER, ENABLE_DRYER,
    MQTT_SLIM_STATE, MQTT_SLIM_EXCLUDE_KEYS,
)
from victron import get_victron
from homeassistant import get_ha
from console_server import start_server as start_console_server, stop_server as stop_console_server, broadcast_line

# MQTT bridge for remote dashboard (optional)
try:
    from mqtt_bridge import get_mqtt_bridge, MQTT_AVAILABLE
except ImportError:
    MQTT_AVAILABLE = False
    get_mqtt_bridge = lambda *a, **kw: None


# =============================================================================
# INVERTER CONTROLLER
# =============================================================================

class InverterController:
    """
    Main controller for grid-zero feed-in management.
    Implements split-phase compensation and various operating modes.
    
    See ARCHITECTURE.md for code structure overview.
    """
    
    # -------------------------------------------------------------------------
    # INITIALIZATION
    # -------------------------------------------------------------------------
    
    def __init__(self, dry_run: Optional[bool] = None):
        # Use config default if not specified via CLI
        self.dry_run = dry_run if dry_run is not None else DRY_RUN
        self.victron = get_victron()
        self.ha = get_ha()
        
        # Load UI configuration
        from ui_config import get_ui_config
        self.ui_config = get_ui_config()
        
        # Process start time
        self.start_time = time.time()
        
        # State
        self.current_setpoint = 0
        self.previous_setpoint = 0
        self.manual_setpoint: Optional[int] = None
        self.delay = 0  # Delay counter for load switching
        self.filtered_gt: Optional[float] = None  # EMA-filtered grid power
        
        # Loop counter
        self.loop_count = 0
        
        # Terminal title update counter
        self.title_update_counter = 0
        
        # Current state for web/console
        self.state: Dict[str, Any] = {}
        
        # Cached D-Bus data (updated less frequently)
        self._cached_mppt_data = {}
        self._cached_tasmota_powers = []
        self._cached_battery_socs = []
        self._cached_inv_state = ""
        
        # Dynamic power limits (can be overridden via web UI)
        self.power_limit_min = POWER_LIMIT_MIN
        self.power_limit_max = POWER_LIMIT_MAX
        
        # Loop interval (can be changed via web UI)
        self.loop_interval = LOOP_INTERVAL
    
    def set_loop_interval(self, interval: float) -> float:
        """Set loop interval in seconds. Returns new interval."""
        # Safety bounds: 0.1s to 5s
        self.loop_interval = max(0.1, min(5.0, interval))
        print(f"\n  LOOP INTERVAL: {self.loop_interval:.2f}s")
        return self.loop_interval
    
    def set_power_limits(self, min_val: int, max_val: int) -> Dict[str, int]:
        """Set dynamic power limits. Returns new limits."""
        self.power_limit_min = max(min_val, -3000)  # Safety floor
        self.power_limit_max = min(max_val, 3000)   # Safety ceiling
        print(f"\n  LIMITS: [{self.power_limit_min}, +{self.power_limit_max}]")
        return {'min': self.power_limit_min, 'max': self.power_limit_max}
    
    def toggle_dry_run(self) -> bool:
        """Toggle dry-run mode. Returns new state."""
        self.dry_run = not self.dry_run
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        print(f"\n{'='*40}")
        print(f"  MODE CHANGED: {mode}")
        print(f"{'='*40}\n")
        return self.dry_run
    
    def toggle_ess_mode(self) -> Dict[str, Any]:
        """Toggle between External control and Optimized without BatteryLife.
        Returns new ESS mode info."""
        current = self.victron.get_ess_mode()
        # Toggle: if external -> optimized, if optimized -> external
        new_external = not current['is_external']
        success = self.victron.set_ess_mode(external=new_external)
        
        if success:
            new_mode = self.victron.get_ess_mode()
            print(f"\n{'='*40}")
            print(f"  ESS MODE: {new_mode['mode_name']}")
            print(f"{'='*40}\n")
            return new_mode
        else:
            print(f"\n  [ERROR] Failed to change ESS mode")
            return current
    
    def get_state(self) -> Dict[str, Any]:
        """Get current state for web interface"""
        return self.state
    
    def set_manual_setpoint(self, value: int) -> bool:
        """Set manual setpoint override"""
        self.manual_setpoint = max(self.power_limit_min, min(self.power_limit_max, value))
        return True
    
    # -------------------------------------------------------------------------
    # SETPOINT CALCULATION (Core Algorithm)
    # -------------------------------------------------------------------------
    
    def calculate_setpoint(self, sys_data: Dict[str, Any]) -> tuple[int, str]:
        """
        Calculate new setpoint based on grid power and operating modes.
        
        SETPOINT CONVENTION (Victron External Control mode):
            Positive setpoint = consume from grid (charge battery)
            Negative setpoint = output to house (discharge battery)
        
        Returns: (setpoint_watts, debug_flags)
        """
        
        # =====================================================================
        # STEP 1: GATHER INPUT DATA
        # =====================================================================
        
        # Grid power per phase and total
        g1 = sys_data['g1']           # L1 grid power (W)
        g2 = sys_data['g2']           # L2 grid power (W)
        gt = sys_data['gt']           # Total grid power (W), positive = importing
        
        # Consumption per phase and total
        t1 = sys_data['t1']           # L1 consumption (W)
        t2 = sys_data['t2']           # L2 consumption (W)
        tt = sys_data['tt']           # Total consumption (W)
        
        # Current inverter output
        inv_power = self.victron.get_inverter_power()
        
        # Solar generation from MPPT controllers (connected to Victron)
        mppt_data = self.victron.get_mppt_data()
        mppt_total = sum(m['w'] for m in mppt_data.values())
        
        # Solar generation from Tasmota microinverters (grid-tied, not controllable)
        tasmota_powers = self.victron.get_tasmota_pv_power()
        tasmota_total = sum(tasmota_powers)
        
        # Total solar from all sources
        pv_total = mppt_total + tasmota_total
        
        # =====================================================================
        # STEP 2: GET HOME ASSISTANT DATA (with fallback protection)
        # =====================================================================
        
        try:
            ev_power = self.ha.get_vue_sensor('ev_charger', 0)
        except Exception:
            ev_power = 0
        
        # =====================================================================
        # STEP 3: GET CONTROL SWITCHES FROM HOME ASSISTANT
        # =====================================================================
        
        only_charging = self.ha.get_boolean('only_charging')
        no_feed = self.ha.get_boolean('no_feed')
        house_support = self.ha.get_boolean('house_support')
        charge_battery = self.ha.get_boolean('charge_battery')
        do_not_supply_charger = self.ha.get_boolean('do_not_supply_charger')
        limit_to_ev = self.ha.get_boolean('set_limit_to_ev_charger')
        
        # Get garage power for EV L1 charging detection
        garage_power = self.ha.get_vue_sensor('garage', 0)
        
        flags = ""
        
        # =====================================================================
        # STEP 4: ADJUST GRID FOR EV EXCLUSION MODE
        # Goal: When do_not_supply_charger is ON, pretend EV doesn't exist
        #       so the algorithm doesn't try to power the EV from battery
        # =====================================================================
        
        effective_gt = gt
        if do_not_supply_charger and ev_power > 100:
            effective_gt = gt - ev_power  # Remove EV consumption from grid calculation
            flags += f"[EV:{int(ev_power)}] "
        
        # =====================================================================
        # STEP 5: BASE CALCULATION - TARGET GRID ZERO
        # Goal: Adjust inverter output to make grid power close to zero
        # 
        # Stability improvements for fast CT meters (VM-3P75CT, Shelly, etc.):
        # 1. EMA filtering - smooth out instantaneous spikes
        # 2. Deadband - ignore small fluctuations
        # 3. Damping - apply only partial correction to prevent overshoot
        # =====================================================================
        
        # Apply Exponential Moving Average (EMA) to smooth grid readings
        # filtered = α * current + (1-α) * previous
        if self.filtered_gt is None:
            self.filtered_gt = effective_gt  # Initialize on first reading
        else:
            self.filtered_gt = (EMA_ALPHA * effective_gt) + ((1 - EMA_ALPHA) * self.filtered_gt)
        
        smoothed_gt = self.filtered_gt
        
        # Deadband: if grid is within acceptable range, keep current setpoint
        # This prevents hunting when grid is already near zero
        if GRID_ZERO_DEADBAND_LOW < smoothed_gt < GRID_ZERO_DEADBAND_HIGH:
            vanew = self.previous_setpoint
            flags += "[~] "
        else:
            # Calculate correction with damping factor
            # Only apply DAMPING_FACTOR (e.g., 70%) of the correction to prevent overshoot
            correction = -smoothed_gt * DAMPING_FACTOR
            vanew = inv_power + correction
        
        # =====================================================================
        # STEP 6: APPLY SPECIAL OPERATING MODES
        # These modes OVERRIDE the base calculation
        # Priority (lowest to highest): only_charging < do_not_supply < no_feed < house_support < charge_battery
        # =====================================================================
        
        # -----------------------------------------------------------------
        # MODE: ONLY_CHARGING
        # Goal: Don't discharge battery - output only what MPPT produces
        # Use case: Preserve battery, use only direct solar
        # Note: Apply inverter efficiency (DC→AC losses ~5-8%)
        # 
        # IMPORTANT: We LIMIT the base calculation, not override it.
        # This prevents grid export when house consumption < MPPT output.
        # -----------------------------------------------------------------
        if only_charging:
            # Max AC output without draining battery
            max_ac_output = int(mppt_total * INVERTER_EFFICIENCY) - SOLAR_OUTPUT_OFFSET
            min_setpoint = -max(0, max_ac_output)  # Most negative allowed
            
            # Limit: don't output more than MPPT allows
            if vanew < min_setpoint:
                vanew = min_setpoint
                flags += f"[OC:{max_ac_output}] "
            else:
                # Base calculation is already within MPPT limits
                flags += f"[OC~] "
        
        # -----------------------------------------------------------------
        # MODE: DO_NOT_SUPPLY_CHARGER (EV exclusion)
        # Goal: Don't let battery power the EV charger
        # Limit: Output cannot exceed MPPT solar generation (only when EV is charging)
        # Note: Grid adjustment in Step 4 makes algorithm ignore EV load
        # -----------------------------------------------------------------
        if do_not_supply_charger:
            if ev_power > 100:
                # HA connected, EV is charging: limit output to MPPT * efficiency
                max_ac_output = max(0, int(mppt_total * INVERTER_EFFICIENCY) - SOLAR_OUTPUT_OFFSET)
                min_setpoint = -max_ac_output  # Most negative allowed
                if vanew < min_setpoint:
                    vanew = min_setpoint
                    flags += f"[NoEV:{max_ac_output}] "
        
        # -----------------------------------------------------------------
        # MODE: LIMIT_TO_EV
        # Goal: When EV is charging, export most solar to grid, keep 500W for battery
        # Trigger: garage (L1 charger) > 1kW OR ev_power (L2 charger) > 1kW
        # Action: setpoint = -(mppt * efficiency - 500)
        # -----------------------------------------------------------------
        BATTERY_RESERVE = 500  # Watts to keep for battery charging
        ev_charging_detected = garage_power > 1000 or ev_power > 1000
        if limit_to_ev:
            if ev_charging_detected:
                ac_output = int(mppt_total * INVERTER_EFFICIENCY)
                export_power = max(0, ac_output - BATTERY_RESERVE)
                vanew = -export_power  # Negative = export to grid
                flags += f"[LimEV:{ac_output}-{BATTERY_RESERVE}] "
        
        # -----------------------------------------------------------------
        # MODE: NO_FEED
        # Goal: Match Tasmota microinverter output exactly
        # Use case: When grid export is not desired/allowed
        # Note: Positive setpoint = consume from grid to offset Tasmota export
        # -----------------------------------------------------------------
        if no_feed:
            vanew = int(tasmota_total)  # Consume what Tasmota exports
            flags += "[NF] "
        
        # -----------------------------------------------------------------
        # MODE: HOUSE_SUPPORT
        # Goal: Tasmota solar minus 300W for house loads
        # Use case: Partial self-consumption mode
        # -----------------------------------------------------------------
        if house_support:
            vanew = int(tasmota_total - 300)
            flags += "[HS] "
        
        # -----------------------------------------------------------------
        # MODE: CHARGE_BATTERY (HIGHEST PRIORITY)
        # Goal: Force battery charging at maximum rate
        # Use case: Prepare battery for evening/night
        # -----------------------------------------------------------------
        if charge_battery:
            vanew = 2200  # Positive = charge from grid
            flags += "[CHG] "
        
        # =====================================================================
        # STEP 7: APPLY SAFETY LIMITS
        # Ensure setpoint stays within hardware/outlet limits
        # =====================================================================
        
        vanew = max(self.power_limit_min, min(self.power_limit_max, vanew))
        
        return int(vanew), flags
    
    def handle_minimize_charging(self, sys_data: Dict[str, Any]):
        """
        Handle minimize_charging logic: turn on/off dump loads
        to consume excess solar or prevent grid import.
        
        This function is non-critical - errors are caught and logged.
        """
        try:
            if self.delay > 0:
                self.delay -= 1
                return
            
            if not self.ha.get_boolean('minimize_charging'):
                return
            
            inverter_state, _ = self.victron.get_inverter_state()
            if inverter_state == 0:  # Inverter is off
                return
            
            net_usage = self.ha.get_sensor('net_usage', 0)
            bp = sys_data.get('bp', 0)  # Battery power (positive = charging)
            
            # If battery charging > 750W and net_usage is low, turn on loads
            if 0 < net_usage < 200 and bp > 750:
                changed = self.ha.control_dump_loads(turn_on=True)
                if changed > 0:
                    self.delay = 6
                    print(f" [MC+{changed}] ", end='')
            
            # If battery discharging or high net usage, turn off loads
            elif bp < -650 or net_usage > 650:
                changed = self.ha.control_dump_loads(turn_on=False)
                if changed > 0:
                    self.delay = 6
                    print(f" [MC-{changed}] ", end='')
        except Exception as e:
            # Non-critical - log and continue
            logger.warning(f"minimize_charging error: {e}")
    
    # -------------------------------------------------------------------------
    # CONSOLE OUTPUT FORMATTING
    # -------------------------------------------------------------------------
    
    def format_console_output(self, sys_data: Dict[str, Any], setpoint: int, flags: str) -> str:
        """Format console output matching bash script style"""
        now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M:%S")
        
        g1, g2, gt = sys_data['g1'], sys_data['g2'], sys_data['gt']
        t1, t2, tt = sys_data['t1'], sys_data['t2'], sys_data['tt']
        bv = sys_data.get('bv', 0)
        bp = sys_data.get('bp', 0)
        
        # Get battery SoC values from D-Bus (cached)
        battery_socs = self._cached_battery_socs or []
        soc1 = int(battery_socs[0]) if len(battery_socs) > 0 else 0
        soc2 = int(battery_socs[1]) if len(battery_socs) > 1 else 0
        comp_v = int(self.ha.get_sensor('compensation_voltage', 0))
        
        # Get inverter state
        _, inv_state_name = self.victron.get_inverter_state()
        
        # Get solar data
        mppt_data = self.victron.get_mppt_data()
        tasmota_powers = self.victron.get_tasmota_pv_power()
        
        mppt_total = sum(m['w'] for m in mppt_data.values())
        tasmota_total = sum(tasmota_powers)
        solar_total = mppt_total + tasmota_total
        
        # Format MPPT breakdown (current with 1 decimal if > 0, else 0A)
        def fmt_current(a):
            if a < 0.05:
                return "0A"
            return f"{a:.1f}A"
        
        mppt_str = '+'.join(f"{int(m['w'])}[{fmt_current(m['a'])}]" for m in mppt_data.values())
        
        # Format Tasmota
        tas_str = '+'.join(str(int(p)) for p in tasmota_powers if p > 0)
        
        # Solar string: total(tasmota+mppt(breakdown))
        solar_str = f"{C.CYAN}{int(solar_total)}("
        if tas_str:
            solar_str += f"{tas_str}+"
        solar_str += f"{int(mppt_total)}({mppt_str})){C.RESET}"
        if solar_total == 0:
            solar_str = f"{C.CYAN}0{C.RESET}"
        
        # Loads (conditional)
        if ENABLE_HA_LOADS:
            loads_parts = []
            for name, key in [('g', 'garage'), ('f', 'fridge'), ('h', 'furnace'), 
                              ('s', 'stove'), ('m', 'microwave'), ('k', 'kitchen_fridge_side'),
                              ('d', 'dishwasher'), ('l', 'lost')]:
                val = int(self.ha.get_sensor(key, 0))
                if val > 19:  # Only show loads > 19W
                    loads_parts.append(f"{val}{name}")
            loads_str = ' '.join(loads_parts) if loads_parts else ""
        else:
            loads_str = ""
        
        # Water level (conditional)
        if ENABLE_WATER:
            water_level = int(self.ha.get_sensor('water_level', 0))
            water_valve = self.ha.water_valve_on
            water_color = C.RED if water_valve else C.YELLOW
            water_str = f"{water_color}{water_level}cm{C.RESET}"
        else:
            water_str = ""
        
        # Car SoC (conditional)
        if ENABLE_EV:
            car_soc = int(self.ha.get_sensor('car_soc', 0))
            car_str = f"{C.YELLOW}{car_soc}%{C.RESET}"
        else:
            car_str = ""
        
        # Time remaining for appliances
        washer = self.ha.get_sensor('washer_time', '')
        dryer = self.ha.get_sensor('dryer_time', '')
        dishwasher_dur = self.ha.get_sensor('dishwasher_duration', '')
        
        # Format times (strip leading zeros)
        def fmt_time(t):
            if not t or t == '0':
                return ''
            t = str(t).lstrip('0:')
            if t.endswith(':00'):
                t = t[:-3]
            return t
        
        washer = fmt_time(washer)
        dryer = fmt_time(dryer)
        
        # Check dishwasher running
        if not self.ha.get_binary_sensor('dishwasher_running'):
            dishwasher_dur = ''
        else:
            dishwasher_dur = fmt_time(dishwasher_dur)
        
        # Build output line (all values rounded appropriately)
        net_usage = int(self.ha.get_sensor('net_usage', gt))
        home_total = int(self.ha.get_sensor('home_total', tt))
        
        # Format: time[flags]>setpoint(prev) g:total[smooth](L1+L2)net tt(L1+L2) tt:home [State]bpW,soc%,b1%,b2% solar loads water car voltage
        # Show smoothed grid value in brackets if different from raw by >10W
        filtered_gt = int(self.filtered_gt) if self.filtered_gt is not None else gt
        smooth_str = f"[{filtered_gt}]" if abs(gt - filtered_gt) > 10 else ""
        
        line = (
            f"{now}{flags}>{C.CYAN}{setpoint}{C.RESET}({self.previous_setpoint}) "
            f"{C.GREEN}g:{gt}{smooth_str}({g1}+{g2}){net_usage}{C.RESET}\t"
            f"{tt}({t1}+{t2}) tt:{home_total} "
            f"{C.YELLOW}[{inv_state_name}]{bp}W,{comp_v}%,{soc1}%,{soc2}%{C.RESET} "
            f"{solar_str} {loads_str} "
            f"{water_str}{car_str}"
            f"{washer}{dryer}{dishwasher_dur} {bv:.2f}"
        )
        
        return line
    
    def update_terminal_title(self):
        """Update terminal title with daily stats"""
        self.title_update_counter += 1
        if self.title_update_counter < 10:
            return
        self.title_update_counter = 0
        
        produced = self.ha.get_sensor('produced_today', 0)
        dollars = self.ha.get_sensor('produced_dollars', 0)
        grid_kwh = self.ha.get_sensor('grid_kwh_today', 0)
        bin_kwh = self.ha.get_sensor('battery_in_today', 0)
        bout_kwh = self.ha.get_sensor('battery_out_today', 0)
        
        title = f"{produced}kW(${dollars})[G:{grid_kwh}kW] B.I:{bin_kwh}kWh,O:{bout_kwh}kWh"
        print(f"\033]2;{title}\007", end='', flush=True)
    
    # -------------------------------------------------------------------------
    # STATE MANAGEMENT (for MQTT/Dashboard)
    # -------------------------------------------------------------------------
    
    def update_state(self, sys_data: Dict[str, Any], setpoint: int, full_update: bool = False):
        """Update internal state for web interface
        
        Args:
            sys_data: System data from D-Bus
            setpoint: Current setpoint
            full_update: If True, refresh all D-Bus cached data (slower)
        """
        # Update cached data
        self._cached_mppt_data = self.victron.get_mppt_data()
        self._cached_tasmota_powers = self.victron.get_tasmota_pv_power()
        self._cached_battery_socs = self.victron.get_battery_chain_socs()
        _, self._cached_inv_state = self.victron.get_inverter_state()
        
        mppt_data = self._cached_mppt_data
        tasmota_powers = self._cached_tasmota_powers
        mppt_total = sum(m['w'] for m in mppt_data.values()) if mppt_data else 0
        tasmota_total = sum(tasmota_powers) if tasmota_powers else 0
        
        # Extract individual MPPT powers (sorted by key)
        mppt_individual = [mppt_data[k]['w'] for k in sorted(mppt_data.keys())] if mppt_data else []
        
        # Daily stats from HA (cached by HA client, no extra calls)
        tasmota_daily = [
            self.ha.get_sensor('tasmota_1_daily', 0),
            self.ha.get_sensor('tasmota_2_daily', 0),
        ]
        mppt_daily = [
            self.ha.get_sensor('mppt_1_daily', 0),
            self.ha.get_sensor('mppt_2_daily', 0),
            self.ha.get_sensor('mppt_3_daily', 0),
        ]
        daily_stats = {
            'produced_today': self.ha.get_sensor('produced_today', 0),
            'produced_dollars': self.ha.get_sensor('produced_dollars', 0),
            'grid_kwh': self.ha.get_sensor('grid_kwh_today', 0),
            'battery_in': self.ha.get_sensor('battery_in_today', 0),
            'battery_out': self.ha.get_sensor('battery_out_today', 0),
            'battery_in_yesterday': self.ha.get_sensor('battery_in_yesterday', 0),
            'battery_out_yesterday': self.ha.get_sensor('battery_out_yesterday', 0),
            'tasmota_daily': tasmota_daily,
            'pv_total_daily': self.ha.get_sensor('pv_total_daily', 0),
            'mppt_daily': mppt_daily,
        }
        
        self.state = {
            **sys_data,
            'setpoint': setpoint,
            'filtered_gt': self.filtered_gt,  # EMA-smoothed grid power
            'dry_run': self.dry_run,
            'mppt_total': mppt_total,
            'tasmota_total': tasmota_total,
            'solar_total': mppt_total + tasmota_total,
            'mppt_data': mppt_data,
            'mppt_individual': mppt_individual,
            'tasmota_individual': tasmota_powers,
            'tasmota_powers': tasmota_powers,
            'inverter_state': self._cached_inv_state,
            'battery_power': sys_data.get('bp', 0),
            'battery_voltage': sys_data.get('bv', 0),
            'battery_current': sys_data.get('bc', 0),
            'battery_soc': sys_data.get('soc', 0) or self.ha.get_sensor('corrected_soc', 0),
            'battery_socs': self._cached_battery_socs,
            'batteries': self.victron.get_all_batteries(),
            'mppt_chargers': self.victron.get_mppt_chargers(),
            # EV data (conditional)
            'ev_power': self.ha.get_vue_sensor('ev_charger', 0) if ENABLE_EV else 0,
            'ev_charging_kw': self.ha.get_sensor('ev_charging_power', 0) if ENABLE_EV else 0,
            'car_soc': self.ha.get_sensor('car_soc', 0) if ENABLE_EV else 0,
            # Water data (conditional)
            'water_level': self.ha.get_sensor('water_level', 0) if ENABLE_WATER else 0,
            'water_valve': self.ha.water_valve_on if ENABLE_WATER else False,
            'pump_switch': self.ha.pump_switch_on if ENABLE_WATER else False,
            # Appliances (conditional)
            'dishwasher_running': self.ha.get_binary_sensor('dishwasher_running') if ENABLE_DISHWASHER else False,
            'dishwasher_duration': self.ha.get_duration_sensor('dishwasher_duration') if ENABLE_DISHWASHER else 0,
            'washer_time': self.ha.get_duration_sensor('washer_time') if ENABLE_WASHER else 0,
            'washer_power': self.ha.washer_power_on if ENABLE_WASHER else False,
            'dryer_time': self.ha.get_duration_sensor('dryer_time') if ENABLE_DRYER else 0,
            'dryer_power': self.ha.dryer_power_on if ENABLE_DRYER else False,
            'laundry_outlet': self.ha.laundry_outlet_on if ENABLE_HA else False,
            'home_recliner': self.ha.home_recliner_on if ENABLE_HA else False,
            'home_garage': self.ha.home_garage_on if ENABLE_HA else False,
            # HA data (conditional)
            'booleans': self.ha.get_all_booleans() if ENABLE_HA else {},
            'daily_stats': daily_stats if ENABLE_HA else {},
            'loads': self.ha.get_all_vue_sensors() if ENABLE_HA_LOADS else {},
            'ha_connected': self.ha.connected if ENABLE_HA else False,
            'ha_uptime': self.ha.uptime if ENABLE_HA else 0,
            # Feature flags for UI
            'features': {
                'ev': ENABLE_EV,
                'water': ENABLE_WATER,
                'ha_loads': ENABLE_HA_LOADS,
                'ha': ENABLE_HA,
                'dishwasher': ENABLE_DISHWASHER,
                'washer': ENABLE_WASHER,
                'dryer': ENABLE_DRYER,
            },
            'limits': {'min': self.power_limit_min, 'max': self.power_limit_max},
            'loop_interval': self.loop_interval,
            'ess_mode': self.victron.get_ess_mode(),
            'uptime': int(time.time() - self.start_time),
            'version': VERSION,
            'ui_config': self.ui_config,
        }
    
    def get_state_for_mqtt(self) -> Dict[str, Any]:
        """Full state for local use; slimmed copy for MQTT when MQTT_SLIM_STATE is True."""
        if not MQTT_SLIM_STATE:
            return self.state
        out = dict(self.state)
        for k in MQTT_SLIM_EXCLUDE_KEYS:
            out.pop(k, None)
        return out
    
    # -------------------------------------------------------------------------
    # CONTROL LOOP
    # -------------------------------------------------------------------------
    
    def run_cycle(self) -> bool:
        """Run one control cycle. Returns False to exit."""
        # Watchdog: kill cycle if it takes more than 5 seconds
        def watchdog_handler(signum, frame):
            raise TimeoutError("Control cycle watchdog timeout")
        
        old_handler = signal.signal(signal.SIGALRM, watchdog_handler)
        signal.alarm(5)  # 5 second watchdog
        
        try:
            # Get system data from D-Bus
            sys_data = self.victron.get_system_data()
            
            # Check for manual setpoint override
            if self.manual_setpoint is not None:
                setpoint = self.manual_setpoint
                flags = "[MANUAL] "
                self.manual_setpoint = None  # Clear after use
            else:
                setpoint, flags = self.calculate_setpoint(sys_data)
            
            # Handle minimize_charging load control
            self.handle_minimize_charging(sys_data)
            
            # Apply setpoint to inverter (only if not in dry-run mode)
            if self.dry_run:
                flags = f"{C.MAGENTA}[DRY]{C.RESET}" + flags
            else:
                self.victron.set_grid_setpoint(setpoint)
            
            # Update screen status
            print(f"\033k{sys_data['gt']}\033\\", end='')
            
            # Format and print console output
            line = self.format_console_output(sys_data, setpoint, flags)
            print(line)
            broadcast_line(line)
            
            # Update state for MQTT bridge
            self.update_state(sys_data, setpoint)
            
            # Update terminal title
            self.update_terminal_title()
            
            # Store for next cycle
            self.previous_setpoint = setpoint
            
            # Handle no_feed mode delay
            try:
                if self.ha.get_boolean('no_feed'):
                    time.sleep(2)
            except Exception:
                pass  # Ignore HA errors for non-critical operations
            
            return True
            
        except KeyboardInterrupt:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            logger.info("KeyboardInterrupt in run_cycle")
            return False
        except TimeoutError as e:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            logger.error("WATCHDOG: Cycle timeout - recovering")
            print(f"\n{C.RED}WATCHDOG: Cycle timeout - recovering...{C.RESET}")
            return True  # Continue, don't exit
        except Exception as e:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            log_exception(f"Error in control cycle: {e}")
            print(f"Error in control cycle: {e}")
            return True
        finally:
            signal.alarm(0)  # Disable watchdog
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

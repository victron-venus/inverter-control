"""
Console UI for Inverter Control
Handles formatting and terminal updates
"""

from datetime import datetime
from typing import Dict, Any, List
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from config import (
    TIMEZONE, Colors as C,
    ENABLE_EV, ENABLE_WATER, ENABLE_HA_LOADS,
    ENABLE_DISHWASHER, ENABLE_WASHER, ENABLE_DRYER, ENABLE_HA
)

class ConsoleUI:
    """Handles terminal output formatting"""
    
    def __init__(self, ha_client, victron_interface):
        self.ha = ha_client
        self.victron = victron_interface
        self.title_update_counter = 0

    def format_line(self, sys_data: Dict[str, Any], setpoint: int, previous_setpoint: int, 
                    flags: str, filtered_gt: float) -> str:
        """Format console output matching original style"""
        now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%H:%M:%S")
        
        g1, g2, gt = sys_data['g1'], sys_data['g2'], sys_data['gt']
        t1, t2, tt = sys_data['t1'], sys_data['t2'], sys_data['tt']
        bv = sys_data.get('bv', 0)
        bp = sys_data.get('bp', 0)
        
        # Get battery SoC values (cached/passed via sys_data or directly from victron)
        # For simplicity, we can use what's in sys_data if we update update_state to include them
        # Or just call victron here if it's fast enough (it's using cached dbus-send anyway)
        battery_socs = sys_data.get('battery_socs', [])
        soc1 = int(battery_socs[0]) if len(battery_socs) > 0 else 0
        soc2 = int(battery_socs[1]) if len(battery_socs) > 1 else 0
        comp_v = int(self.ha.get_sensor('compensation_voltage', 0))
        
        # Get inverter state
        _, inv_state_name = self.victron.get_inverter_state()
        
        # Get solar data
        mppt_data = sys_data.get('mppt_data', {})
        tasmota_powers = sys_data.get('tasmota_powers', [])
        
        mppt_total = sum(m['w'] for m in mppt_data.values())
        tasmota_total = sum(tasmota_powers)
        solar_total = mppt_total + tasmota_total
        
        # Format MPPT breakdown
        def fmt_current(a):
            if a < 0.05:
                return "0A"
            return f"{a:.1f}A"
        
        mppt_str = '+'.join(f"{int(m['w'])}[{fmt_current(m['a'])}]" for m in mppt_data.values())
        
        # Format Tasmota
        tas_str = '+'.join(str(int(p)) for p in tasmota_powers if p > 0)
        
        # Solar string
        solar_str = f"{C.CYAN}{int(solar_total)}("
        if tas_str:
            solar_str += f"{tas_str}+"
        solar_str += f"{int(mppt_total)}({mppt_str})){C.RESET}"
        if solar_total == 0:
            solar_str = f"{C.CYAN}0{C.RESET}"
        
        # Loads
        loads_str = ""
        if ENABLE_HA_LOADS:
            loads_parts = []
            for name, key in [('g', 'garage'), ('f', 'fridge'), ('h', 'furnace'), 
                              ('s', 'stove'), ('m', 'microwave'), ('k', 'kitchen_fridge_side'),
                              ('d', 'dishwasher'), ('l', 'lost')]:
                val = int(self.ha.get_sensor(key, 0))
                if val > 19:
                    loads_parts.append(f"{val}{name}")
            loads_str = ' '.join(loads_parts) if loads_parts else ""
        
        # Water level
        water_str = ""
        if ENABLE_WATER:
            water_level = int(self.ha.get_sensor('water_level', 0))
            water_valve = self.ha.water_valve_on
            water_color = C.RED if water_valve else C.YELLOW
            water_str = f"{water_color}{water_level}cm{C.RESET}"
        
        # Car SoC
        car_str = ""
        if ENABLE_EV:
            car_soc = int(self.ha.get_sensor('car_soc', 0))
            car_str = f"{C.YELLOW}{car_soc}%{C.RESET}"
        
        # Appliances
        washer = fmt_appliance_time(self.ha.get_sensor('washer_time', ''))
        dryer = fmt_appliance_time(self.ha.get_sensor('dryer_time', ''))
        dishwasher_dur = ""
        if ENABLE_DISHWASHER and self.ha.get_binary_sensor('dishwasher_running'):
            dishwasher_dur = fmt_appliance_time(self.ha.get_sensor('dishwasher_duration', ''))
        
        # Totals
        net_usage = int(self.ha.get_sensor('net_usage', gt))
        home_total = int(self.ha.get_sensor('home_total', tt))
        
        # Filtered grid power display
        smooth_str = f"[{int(filtered_gt)}]" if abs(gt - filtered_gt) > 10 else ""
        
        line = (
            f"{now}{flags}>{C.CYAN}{setpoint}{C.RESET}({previous_setpoint}) "
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

def fmt_appliance_time(t):
    """Format appliance time (strip leading zeros)"""
    if not t or t == '0':
        return ''
    t = str(t).lstrip('0:')
    if t.endswith(':00'):
        t = t[:-3]
    return t

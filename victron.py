#!/usr/bin/env python3
"""
Victron D-Bus Interface
Fast D-Bus access for grid control and monitoring
"""

import subprocess
import re
import signal
import logging
import traceback
from typing import Optional, Dict, Any, Tuple
from config import INVERTER_STATES, TASMOTA_DBUS_SERVICES

logger = logging.getLogger('inverter-control')

# Timeout handler for stuck subprocesses
class TimeoutError(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutError("D-Bus call timed out")


class VictronDBus:
    """
    Fast D-Bus interface for Victron system.
    Uses subprocess calls to dbus-send for maximum speed on Venus OS.
    """
    
    # Auto-rescan thresholds
    RESCAN_ERROR_THRESHOLD = 5      # Rescan after N consecutive errors
    RESCAN_INTERVAL_SECONDS = 300   # Rescan every 5 minutes regardless
    
    def __init__(self):
        self._vebus_service: Optional[str] = None
        self._mppt_services: list = []
        self._consecutive_errors: int = 0
        self._last_scan_time: float = 0
        self._last_success_time: float = 0
        self._discover_services()
    
    def _discover_services(self):
        """Discover VE.Bus and MPPT services"""
        import time
        self._last_scan_time = time.time()
        old_vebus = self._vebus_service
        
        try:
            result = subprocess.run(
                ['dbus', '-y'],
                capture_output=True, text=True, timeout=2
            )
            lines = result.stdout.strip().split('\n')
            
            self._vebus_service = None
            self._mppt_services = []
            
            for line in lines:
                if 'com.victronenergy.vebus' in line:
                    self._vebus_service = line.strip()
                elif 'com.victronenergy.solarcharger' in line:
                    self._mppt_services.append(line.strip())
            
            self._mppt_services.sort()
            
            # Log if service changed
            if old_vebus and self._vebus_service and old_vebus != self._vebus_service:
                print(f"  [D-Bus] VE.Bus service changed: {old_vebus} -> {self._vebus_service}")
            elif not old_vebus and self._vebus_service:
                print(f"  [D-Bus] VE.Bus service found: {self._vebus_service}")
            
            self._consecutive_errors = 0
            
        except Exception as e:
            print(f"Error discovering D-Bus services: {e}")
    
    def _check_rescan_needed(self) -> bool:
        """Check if D-Bus rescan is needed and perform it if so"""
        import time
        now = time.time()
        
        # Rescan if too many consecutive errors
        if self._consecutive_errors >= self.RESCAN_ERROR_THRESHOLD:
            print(f"  [D-Bus] Rescanning after {self._consecutive_errors} errors...")
            self._discover_services()
            return True
        
        # Periodic rescan
        if now - self._last_scan_time > self.RESCAN_INTERVAL_SECONDS:
            self._discover_services()
            return True
        
        return False
    
    @property
    def vebus_service(self) -> Optional[str]:
        return self._vebus_service
    
    @property
    def mppt_services(self) -> list:
        return self._mppt_services
    
    def _safe_subprocess(self, cmd: list, timeout: float = 0.3) -> Optional[str]:
        """Run subprocess with strict timeout and error handling"""
        try:
            # Use start_new_session to be able to kill the whole process group
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                start_new_session=True
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.strip()
        except subprocess.TimeoutExpired:
            pass  # Timeout is expected sometimes
        except Exception:
            pass
        return None
    
    def _dbus_get(self, service: str, path: str) -> Optional[str]:
        """Get a single value from D-Bus (fast)"""
        import time
        
        # Check if rescan needed before operation
        self._check_rescan_needed()
        
        result = self._safe_subprocess(
            ['dbus-send', '--system', '--print-reply=literal',
             f'--dest={service}', path,
             'com.victronenergy.BusItem.GetValue'],
            timeout=0.3
        )
        if result:
            parts = result.split()
            if parts:
                self._consecutive_errors = 0
                self._last_success_time = time.time()
                return parts[-1]
        
        # Track error
        self._consecutive_errors += 1
        return None
    
    def _dbus_set(self, service: str, path: str, value: int, value_type: str = 'int16') -> bool:
        """Set a value on D-Bus"""
        import time
        self._check_rescan_needed()
        
        result = self._safe_subprocess(
            ['dbus-send', '--system', '--type=method_call',
             f'--dest={service}', path,
             'com.victronenergy.BusItem.SetValue',
             f'variant:{value_type}:{value}'],
            timeout=0.3
        )
        if result is not None:
            self._consecutive_errors = 0
            self._last_success_time = time.time()
            return True
        
        self._consecutive_errors += 1
        return False
    
    def get_system_data(self) -> Dict[str, Any]:
        """
        Get all system data in one D-Bus call (fastest method).
        Returns dict with grid, consumption, battery, and solar data.
        """
        data = {
            'g1': 0, 'g2': 0, 'gt': 0,      # Grid L1, L2, Total
            't1': 0, 't2': 0, 'tt': 0,      # Consumption L1, L2, Total
            'bv': 0.0,                       # Battery voltage
            'bc': 0.0,                       # Battery current
            'bp': 0,                         # Battery power
            'pv_total': 0,                   # Total PV power
        }
        
        output = self._safe_subprocess(
            ['dbus-send', '--system', '--print-reply',
             '--dest=com.victronenergy.system', '/',
             'com.victronenergy.BusItem.GetValue'],
            timeout=0.5
        )
        
        if not output:
            return data
        
        # Parse with regex for speed
        patterns = {
            'g1': r'Ac/Grid/L1/Power.*?\n.*?(\-?[\d.]+)',
            'g2': r'Ac/Grid/L2/Power.*?\n.*?(\-?[\d.]+)',
            't1': r'Ac/Consumption/L1/Power.*?\n.*?(\-?[\d.]+)',
            't2': r'Ac/Consumption/L2/Power.*?\n.*?(\-?[\d.]+)',
            'bv': r'Dc/Battery/Voltage".*?\n.*?([\d.]+)',
            'bc': r'Dc/Battery/Current".*?\n.*?(\-?[\d.]+)',
            'bp': r'Dc/Battery/Power.*?\n.*?(\-?[\d.]+)',
            'pv_total': r'Dc/Pv/Power.*?\n.*?([\d.]+)',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, output, re.DOTALL)
            if match:
                try:
                    val = float(match.group(1))
                    data[key] = int(val) if key not in ('bv', 'bc') else val
                except:
                    pass
        
        data['gt'] = data['g1'] + data['g2']
        data['tt'] = data['t1'] + data['t2']
        
        return data
    
    def get_inverter_state(self) -> Tuple[int, str]:
        """Get inverter state code and description"""
        if not self._vebus_service:
            return 0, "Unknown"
        
        val = self._dbus_get(self._vebus_service, '/State')
        if val:
            try:
                code = int(val)
                return code, INVERTER_STATES.get(code, f"? ({code})")
            except:
                pass
        return 0, "Unknown"
    
    def get_inverter_power(self) -> int:
        """Get current inverter AC output power"""
        if not self._vebus_service:
            return 0
        
        # Try specific device path first (faster)
        val = self._dbus_get(self._vebus_service, '/Devices/0/Ac/Inverter/P')
        if val:
            try:
                return int(float(val))
            except:
                pass
        return 0
    
    def get_ac_in_power(self) -> int:
        """Get AC input power (from grid)"""
        if not self._vebus_service:
            return 0
        
        val = self._dbus_get(self._vebus_service, '/Ac/ActiveIn/L1/P')
        if val:
            try:
                return int(float(val))
            except:
                pass
        return 0
    
    def set_grid_setpoint(self, watts: int) -> bool:
        """Set the grid power setpoint (Hub4/L1/AcPowerSetpoint)"""
        if not self._vebus_service:
            return False
        
        return self._dbus_set(
            self._vebus_service,
            '/Hub4/L1/AcPowerSetpoint',
            watts,
            'int16'
        )
    
    def get_mppt_data(self) -> Dict[str, Dict[str, float]]:
        """Get power and current from all MPPT chargers"""
        data = {}
        
        for i, service in enumerate(self._mppt_services):
            mppt_data = {'w': 0.0, 'a': 0.0}
            
            # Get power
            val = self._dbus_get(service, '/Yield/Power')
            if val:
                try:
                    mppt_data['w'] = float(val)
                except:
                    pass
            
            # Get current
            val = self._dbus_get(service, '/Dc/0/Current')
            if val:
                try:
                    mppt_data['a'] = float(val)
                except:
                    pass
            
            data[f'mppt{i}'] = mppt_data
        
        return data
    
    def get_tasmota_pv_power(self) -> list:
        """Get power from Tasmota PV inverters via D-Bus"""
        powers = []
        
        for service in TASMOTA_DBUS_SERVICES:
            val = self._dbus_get(service, '/Ac/Power')
            if val:
                try:
                    powers.append(float(val))
                except:
                    powers.append(0.0)
            else:
                powers.append(0.0)
        
        return powers
    
    def get_battery_soc(self) -> Optional[float]:
        """Get battery SoC from system"""
        val = self._dbus_get('com.victronenergy.system', '/Dc/Battery/Soc')
        if val:
            try:
                return float(val)
            except:
                pass
        return None
    
    def get_battery_chain_socs(self) -> list:
        """Get SoC for each battery chain from D-Bus
        
        Returns list of SoC values for:
        - mqtt_chain1 (first series)
        - mqtt_chain2 (second series)
        """
        battery_services = [
            'com.victronenergy.battery.mqtt_chain1',
            'com.victronenergy.battery.mqtt_chain2',
        ]
        
        socs = []
        for service in battery_services:
            val = self._dbus_get(service, '/Soc')
            if val:
                try:
                    socs.append(float(val))
                except:
                    socs.append(0.0)
            else:
                socs.append(0.0)
        
        return socs
    
    def get_ess_mode(self) -> Dict[str, Any]:
        """Get current ESS mode
        
        Returns dict with:
        - hub4_mode: 1=ESS, 3=External control
        - battery_life_state: 0=Optimized without BatteryLife, 1-8=BatteryLife, 9=Keep charged
        - mode_name: Human readable name
        - is_external: True if External control mode
        """
        hub4_mode = 0
        bl_state = 0
        
        val = self._dbus_get('com.victronenergy.settings', '/Settings/CGwacs/Hub4Mode')
        if val:
            try:
                hub4_mode = int(val)
            except:
                pass
        
        val = self._dbus_get('com.victronenergy.settings', '/Settings/CGwacs/BatteryLife/State')
        if val:
            try:
                bl_state = int(val)
            except:
                pass
        
        # Determine mode name
        # BatteryLife states:
        # 0 or 10 = Optimized without BatteryLife (BatteryLife disabled)
        # 1-8 = Optimized with BatteryLife (various SoC stages)
        # 9 = Keep batteries charged
        if hub4_mode == 3:
            mode_name = "External control"
            is_external = True
        elif hub4_mode == 1:
            is_external = False
            if bl_state == 0 or bl_state == 10:
                mode_name = "Optimized without BatteryLife"
            elif bl_state == 9:
                mode_name = "Keep batteries charged"
            else:
                mode_name = "Optimized (BatteryLife)"
        else:
            mode_name = f"Unknown ({hub4_mode})"
            is_external = False
        
        return {
            'hub4_mode': hub4_mode,
            'battery_life_state': bl_state,
            'mode_name': mode_name,
            'is_external': is_external
        }
    
    def set_ess_mode(self, external: bool) -> bool:
        """Set ESS mode
        
        Args:
            external: True for External control, False for Optimized without BatteryLife
        
        Returns True if successful
        """
        if external:
            # External control: Hub4Mode = 3
            return self._dbus_set(
                'com.victronenergy.settings',
                '/Settings/CGwacs/Hub4Mode',
                3,
                'int32'
            )
        else:
            # Optimized without BatteryLife: Hub4Mode = 1, BatteryLife/State = 0
            success1 = self._dbus_set(
                'com.victronenergy.settings',
                '/Settings/CGwacs/Hub4Mode',
                1,
                'int32'
            )
            success2 = self._dbus_set(
                'com.victronenergy.settings',
                '/Settings/CGwacs/BatteryLife/State',
                0,
                'int32'
            )
            return success1 and success2
    
    def get_all_batteries(self) -> list:
        """Get detailed data for all battery chains including SmartShunt
        
        Returns list of dicts with: name, voltage, current, power, soc, state,
        time_to_go (formatted), time_to_go_sec (optional).
        """
        battery_services = [
            ('com.victronenergy.battery.dbus-mqtt-chain1', 'JBD Chain 1'),
            ('com.victronenergy.battery.dbus-mqtt-chain2', 'JBD Chain 2'),
            ('com.victronenergy.battery.virtual_chain', 'Virtual Battery'),
        ]
        
        batteries = []
        for service, name in battery_services:
            battery = {'name': name, 'voltage': 0.0, 'soc': 0.0, 'state': 'Unknown'}
            
            # Voltage
            val = self._dbus_get(service, '/Dc/0/Voltage')
            if val:
                try:
                    battery['voltage'] = float(val)
                except:
                    pass
            
            # Current
            val = self._dbus_get(service, '/Dc/0/Current')
            if val:
                try:
                    battery['current'] = float(val)
                except:
                    pass
            
            # Power
            val = self._dbus_get(service, '/Dc/0/Power')
            if val:
                try:
                    battery['power'] = float(val)
                except:
                    pass
            
            # SoC
            val = self._dbus_get(service, '/Soc')
            if val:
                try:
                    battery['soc'] = float(val)
                except:
                    pass
            
            # State (from /Info/State or derive from current)
            current = battery.get('current', 0)
            if current is not None:
                if current > 0.5:
                    battery['state'] = 'Charging'
                elif current < -0.5:
                    battery['state'] = 'Discharging'
                else:
                    battery['state'] = 'Idle'
            else:
                battery['state'] = 'Idle'

            # Time remaining (seconds) — Victron /TimeToGo (same basis as VRM)
            battery['time_to_go'] = ''
            battery['time_to_go_sec'] = None
            ttg_raw = self._dbus_get(service, '/TimeToGo')
            if ttg_raw is not None:
                try:
                    ttg_sec = max(0, int(float(ttg_raw)))
                    battery['time_to_go_sec'] = ttg_sec
                    max_reasonable = 86400 * 14  # ignore stale / idle huge values
                    if (
                        battery['state'] in ('Charging', 'Discharging')
                        and 0 < ttg_sec < max_reasonable
                    ):
                        h = ttg_sec // 3600
                        m = (ttg_sec % 3600) // 60
                        if h > 0:
                            battery['time_to_go'] = f'{h}h {m:02d}m'
                        else:
                            battery['time_to_go'] = f'{m}m'
                except (TypeError, ValueError):
                    pass
            
            batteries.append(battery)
        
        return batteries
    
    def get_mppt_chargers(self) -> list:
        """Get detailed data for all MPPT chargers
        
        Returns list of dicts with: name, pv_voltage, current, power
        """
        chargers = []
        
        for i, service in enumerate(self._mppt_services):
            # Extract MPPT number from service name (e.g. "ttyUSB0:290" -> "290")
            parts = service.split(':')
            name = f"MPPT-{parts[1]}" if len(parts) > 1 else f"MPPT-{i}"
            
            charger = {'name': name, 'pv_voltage': 0.0, 'current': 0.0, 'power': 0.0}
            
            # PV Voltage
            val = self._dbus_get(service, '/Pv/V')
            if val:
                try:
                    charger['pv_voltage'] = float(val)
                except:
                    pass
            
            # Current
            val = self._dbus_get(service, '/Dc/0/Current')
            if val:
                try:
                    charger['current'] = float(val)
                except:
                    pass
            
            # Power
            val = self._dbus_get(service, '/Yield/Power')
            if val:
                try:
                    charger['power'] = float(val)
                except:
                    pass
            
            chargers.append(charger)
        
        return chargers


# Singleton instance
_victron: Optional[VictronDBus] = None

def get_victron() -> VictronDBus:
    """Get or create Victron D-Bus interface"""
    global _victron
    if _victron is None:
        _victron = VictronDBus()
    return _victron

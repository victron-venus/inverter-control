#!/usr/bin/env python3
"""
Home Assistant Integration
API access with caching and fallback for unreliable connections
"""

import requests
import threading
import time
import logging
from typing import Dict, Any, Optional
from config import (
    HA_URL, HA_TOKEN, HA_TIMEOUT, HA_POLL_INTERVAL,
    HA_SENSORS, HA_BOOLEANS, HA_BINARY_SENSORS,
    HA_DUMP_LOADS, HA_WATER_VALVE, HA_PUMP_SWITCH, VUE_SENSORS,
    ENABLE_DISHWASHER, ENABLE_WASHER, ENABLE_DRYER, ENABLE_WATER,
    HA_WASHER_POWER, HA_DRYER_POWER
)

logger = logging.getLogger('inverter-control')


class HomeAssistantClient:
    """
    Home Assistant API client with caching and fallback.
    Runs polling in background thread.
    Uses last known values when HA is unreachable.
    """
    
    # Circuit breaker settings
    CIRCUIT_OPEN_THRESHOLD = 5      # Open circuit after N consecutive failures
    CIRCUIT_RESET_TIMEOUT = 60      # Try again after N seconds
    
    def __init__(self):
        # Use session for connection pooling (reuses TCP connections)
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json"
        })
        # Configure connection pool
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=2,
            pool_maxsize=5,
            max_retries=0  # We handle retries ourselves
        )
        self._session.mount('http://', adapter)
        self._session.mount('https://', adapter)
        
        # Cached values (persist until HA reconnects)
        self._sensors: Dict[str, Any] = {k: 0 for k in HA_SENSORS}
        self._vue_sensors: Dict[str, Any] = {k: 0 for k in VUE_SENSORS}
        self._booleans: Dict[str, bool] = {k: False for k in HA_BOOLEANS}
        self._binary_sensors: Dict[str, bool] = {k: False for k in HA_BINARY_SENSORS}
        self._water_valve: bool = False
        self._pump_switch: bool = False
        self._washer_power: bool = False
        self._dryer_power: bool = False
        
        # Connection status
        self._connected = False
        self._last_update = 0
        self._last_error = ""
        self._last_error_log = 0  # Throttle error logging
        
        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_open_time = 0
        
        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
    
    def start(self):
        """Start background polling thread"""
        if self._running:
            return
        
        self._running = True
        self._start_time = time.time()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
    
    @property
    def uptime(self) -> int:
        """Return HA poller uptime in seconds"""
        if hasattr(self, '_start_time'):
            return int(time.time() - self._start_time)
        return 0
    
    def stop(self):
        """Stop background polling and cleanup"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        # Close session to release connections
        try:
            self._session.close()
        except Exception:
            pass
    
    def _get_state(self, entity_id: str) -> Optional[str]:
        """Get entity state from HA"""
        try:
            response = self._session.get(
                f"{HA_URL}/api/states/{entity_id}",
                timeout=(3, HA_TIMEOUT)  # (connect_timeout, read_timeout)
            )
            if response.status_code == 200:
                return response.json().get('state')
        except (requests.exceptions.RequestException, ValueError):
            pass
        return None
    
    def _parse_numeric(self, value: str, default: Any = 0) -> Any:
        """Parse numeric value, handle 'unavailable', 'unknown', etc."""
        if value in (None, 'unavailable', 'unknown', 'None', ''):
            return default
        try:
            # Try int first
            if '.' not in str(value):
                return int(value)
            return float(value)
        except:
            return default
    
    def _parse_duration(self, value: str) -> int:
        """Parse duration in HH:MM:SS or MM:SS format to minutes"""
        if value in (None, 'unavailable', 'unknown', 'None', ''):
            return 0
        try:
            # Try numeric first
            return int(float(value))
        except:
            pass
        try:
            # Try HH:MM:SS or MM:SS format
            parts = str(value).split(':')
            if len(parts) == 3:
                hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
                return hours * 60 + mins + (1 if secs >= 30 else 0)
            elif len(parts) == 2:
                mins, secs = int(parts[0]), int(parts[1])
                return mins + (1 if secs >= 30 else 0)
        except:
            pass
        return 0
    
    def _poll_loop(self):
        """Background polling loop with circuit breaker"""
        while self._running:
            now = time.time()
            
            # Circuit breaker: skip polling if circuit is open
            if self._circuit_open:
                if now - self._circuit_open_time > self.CIRCUIT_RESET_TIMEOUT:
                    # Try to reset circuit
                    self._circuit_open = False
                    logger.info("HA circuit breaker: attempting reset")
                else:
                    time.sleep(HA_POLL_INTERVAL)
                    continue
            
            try:
                self._poll_all()
                self._connected = True
                self._last_update = now
                self._last_error = ""
                self._consecutive_failures = 0
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                self._consecutive_failures += 1
                
                # Open circuit breaker after threshold
                if self._consecutive_failures >= self.CIRCUIT_OPEN_THRESHOLD:
                    self._circuit_open = True
                    self._circuit_open_time = now
                    logger.warning(f"HA circuit breaker OPEN after {self._consecutive_failures} failures")
                
                # Throttle error logging to once per minute
                if now - self._last_error_log > 60:
                    logger.warning(f"HA poll failed ({self._consecutive_failures}x): {e}")
                    self._last_error_log = now
            
            time.sleep(HA_POLL_INTERVAL)
    
    def _poll_all(self):
        """Poll all entities from HA"""
        # Use template API for batch fetch (much faster)
        template = self._build_template()
        
        try:
            response = self._session.post(
                f"{HA_URL}/api/template",
                json={"template": template},
                timeout=(3, HA_TIMEOUT)  # (connect_timeout, read_timeout)
            )
        except requests.exceptions.Timeout:
            raise Exception("HA timeout")
        except requests.exceptions.ConnectionError:
            raise Exception("HA connection failed")
        
        if response.status_code != 200:
            raise Exception(f"HA API error: {response.status_code}")
        
        data = response.json()
        if not isinstance(data, dict):
            raise Exception("Invalid response format")
        
        with self._lock:
            # Sensors that should be stored as raw strings (duration format HH:MM:SS)
            duration_sensors = {'dishwasher_duration', 'washer_time', 'dryer_time'}
            
            # Parse sensors
            for key in HA_SENSORS:
                if key in data:
                    if key in duration_sensors:
                        self._sensors[key] = data[key]  # Store raw for duration parsing
                    else:
                        self._sensors[key] = self._parse_numeric(data[key])
            
            # Parse VUE sensors
            for key in VUE_SENSORS:
                if key in data:
                    self._vue_sensors[key] = self._parse_numeric(data[key])
            
            # Parse booleans
            for key in HA_BOOLEANS:
                if key in data:
                    self._booleans[key] = data[key] == 'on'
            
            # Parse binary sensors
            for key in HA_BINARY_SENSORS:
                if key in data:
                    self._binary_sensors[key] = data[key] == 'on'
            
            # Water valve
            if 'water_valve' in data:
                self._water_valve = data['water_valve'] == 'on'
            
            # Pump switch
            if 'pump_switch' in data:
                self._pump_switch = data['pump_switch'] == 'on'
            
            # Washer/Dryer power switches
            if 'washer_power' in data:
                self._washer_power = data['washer_power'] == 'on'
            if 'dryer_power' in data:
                self._dryer_power = data['dryer_power'] == 'on'
    
    def _build_template(self) -> str:
        """Build Jinja2 template for batch fetch"""
        # Keys to skip based on disabled features
        skip_sensors = set()
        skip_binary = set()
        
        if not ENABLE_DISHWASHER:
            skip_sensors.add('dishwasher_duration')
            skip_binary.add('dishwasher_running')
        if not ENABLE_WASHER:
            skip_sensors.add('washer_time')
        if not ENABLE_DRYER:
            skip_sensors.add('dryer_time')
        if not ENABLE_WATER:
            skip_sensors.add('water_level')
        
        parts = ['{']
        items = []
        
        # Sensors (skip disabled)
        for key, entity in HA_SENSORS.items():
            if key not in skip_sensors:
                items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')
        
        # VUE sensors
        for key, entity in VUE_SENSORS.items():
            items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')
        
        # Booleans
        for key, entity in HA_BOOLEANS.items():
            items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')
        
        # Binary sensors (skip disabled)
        for key, entity in HA_BINARY_SENSORS.items():
            if key not in skip_binary:
                items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')
        
        # Water valve and pump (only if water enabled)
        if ENABLE_WATER:
            items.append(f'  "water_valve": "{{{{ states("{HA_WATER_VALVE}") }}}}"')
            items.append(f'  "pump_switch": "{{{{ states("{HA_PUMP_SWITCH}") }}}}"')
        
        # Washer/Dryer power switches
        if ENABLE_WASHER and HA_WASHER_POWER:
            items.append(f'  "washer_power": "{{{{ states("{HA_WASHER_POWER}") }}}}"')
        if ENABLE_DRYER and HA_DRYER_POWER:
            items.append(f'  "dryer_power": "{{{{ states("{HA_DRYER_POWER}") }}}}"')
        
        parts.append(',\n'.join(items))
        parts.append('}')
        return '\n'.join(parts)
    
    # === Public API ===
    
    @property
    def connected(self) -> bool:
        return self._connected
    
    @property
    def last_update(self) -> float:
        return self._last_update
    
    @property
    def last_error(self) -> str:
        return self._last_error
    
    def get_sensor(self, key: str, default: Any = 0) -> Any:
        """Get cached sensor value"""
        with self._lock:
            return self._sensors.get(key, default)
    
    def get_duration_sensor(self, key: str) -> int:
        """Get cached sensor value and parse as duration (HH:MM:SS) to minutes"""
        with self._lock:
            raw = self._sensors.get(key)
        return self._parse_duration(raw)
    
    def get_vue_sensor(self, key: str, default: Any = 0) -> Any:
        """Get cached VUE sensor value"""
        with self._lock:
            return self._vue_sensors.get(key, default)
    
    def get_all_vue_sensors(self) -> Dict[str, Any]:
        """Get copy of all VUE sensor values"""
        with self._lock:
            return dict(self._vue_sensors)
    
    def get_boolean(self, key: str) -> bool:
        """Get cached boolean value"""
        with self._lock:
            return self._booleans.get(key, False)
    
    def get_binary_sensor(self, key: str) -> bool:
        """Get cached binary sensor value"""
        with self._lock:
            return self._binary_sensors.get(key, False)
    
    @property
    def water_valve_on(self) -> bool:
        with self._lock:
            return self._water_valve
    
    @property
    def pump_switch_on(self) -> bool:
        with self._lock:
            return self._pump_switch
    
    @property
    def washer_power_on(self) -> bool:
        with self._lock:
            return self._washer_power
    
    @property
    def dryer_power_on(self) -> bool:
        with self._lock:
            return self._dryer_power
    
    def get_all_sensors(self) -> Dict[str, Any]:
        """Get copy of all sensor values"""
        with self._lock:
            return dict(self._sensors)
    
    def get_all_booleans(self) -> Dict[str, bool]:
        """Get copy of all boolean values"""
        with self._lock:
            return dict(self._booleans)
    
    # === Control Methods ===
    
    def toggle_entity(self, entity_id: str) -> bool:
        """Toggle a switch or input_boolean"""
        try:
            domain = entity_id.split('.')[0]
            response = self._session.post(
                f"{HA_URL}/api/services/{domain}/toggle",
                json={"entity_id": entity_id},
                timeout=(3, HA_TIMEOUT)
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Toggle {entity_id} failed: {e}")
            return False
    
    def press_button(self, entity_id: str) -> bool:
        """Press a button entity"""
        try:
            response = self._session.post(
                f"{HA_URL}/api/services/button/press",
                json={"entity_id": entity_id},
                timeout=(3, HA_TIMEOUT)
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Press {entity_id} failed: {e}")
            return False
    
    def turn_on(self, entity_id: str) -> bool:
        """Turn on a switch or light"""
        try:
            domain = entity_id.split('.')[0]
            response = self._session.post(
                f"{HA_URL}/api/services/{domain}/turn_on",
                json={"entity_id": entity_id},
                timeout=(3, HA_TIMEOUT)
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Turn on {entity_id} failed: {e}")
            return False
    
    def turn_off(self, entity_id: str) -> bool:
        """Turn off a switch or light"""
        try:
            domain = entity_id.split('.')[0]
            response = self._session.post(
                f"{HA_URL}/api/services/{domain}/turn_off",
                json={"entity_id": entity_id},
                timeout=(3, HA_TIMEOUT)
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Turn off {entity_id} failed: {e}")
            return False
    
    def control_dump_loads(self, turn_on: bool) -> int:
        """Control all dump loads for minimize_charging. Returns count of changed."""
        changed = 0
        for entity in HA_DUMP_LOADS:
            if turn_on:
                if self.turn_on(entity):
                    changed += 1
            else:
                if self.turn_off(entity):
                    changed += 1
        return changed


# Singleton instance
_ha_client: Optional[HomeAssistantClient] = None

def get_ha() -> HomeAssistantClient:
    """Get or create HA client"""
    global _ha_client
    if _ha_client is None:
        _ha_client = HomeAssistantClient()
        _ha_client.start()
    return _ha_client

#!/usr/bin/env python3
"""
Home Assistant Integration
API access with caching and fallback for unreliable connections
"""

import logging
import re
import threading
import time
from typing import Any

import requests
import urllib3

from .config import (
    ENABLE_DISHWASHER,
    ENABLE_DRYER,
    ENABLE_WASHER,
    ENABLE_WATER,
    HA_BINARY_SENSORS,
    HA_BOOLEANS,
    HA_DRYER_POWER,
    HA_DUMP_LOADS,
    HA_LAUNDRY_OUTLET,
    HA_POLL_INTERVAL,
    HA_PUMP_SWITCH,
    HA_SENSORS,
    HA_TIMEOUT,
    HA_TOKEN,
    HA_URL,
    HA_WASHER_POWER,
    HA_WATER_VALVE,
    VUE_SENSORS,
)

logger = logging.getLogger("inverter-control")

# Disable insecure request warnings for local HA instance (http:// is intentional)
# nosec B310

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class HomeAssistantError(Exception):
    """Base exception for Home Assistant errors"""


class HomeAssistantTimeoutError(HomeAssistantError):
    """Raised when HA request times out"""


class HomeAssistantConnectionError(HomeAssistantError):
    """Raised when HA connection fails"""


class HomeAssistantAPIError(HomeAssistantError):
    """Raised when HA returns non-200 status"""


class HomeAssistantResponseError(HomeAssistantError):
    """Raised when HA returns invalid response format"""


class HomeAssistantClient:  # pylint: disable=too-many-public-methods
    """
    Home Assistant API client with caching and fallback.
    Runs polling in background thread.
    Uses last known values when HA is unreachable.
    """

    # Circuit breaker settings
    CIRCUIT_OPEN_THRESHOLD = 5  # Open circuit after N consecutive failures
    CIRCUIT_RESET_TIMEOUT = 60  # Try again after N seconds

    def __init__(self):
        # Use session for connection pooling (reuses TCP connections)
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        )
        # Configure connection pool for HA (local network, http or https)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=2,
            pool_maxsize=5,
            max_retries=0,  # We handle retries ourselves
        )
        # http:// mount required for local HA instances (no SSL on local network)
        self._session.mount("http://", adapter)  # NOSONAR python:S5332
        self._session.mount("https://", adapter)

        # Cached values (persist until HA reconnects)
        self._sensors: dict[str, Any] = dict.fromkeys(HA_SENSORS, 0)
        self._vue_sensors: dict[str, Any] = dict.fromkeys(VUE_SENSORS, 0)
        self._booleans: dict[str, bool] = dict.fromkeys(HA_BOOLEANS, False)
        self._binary_sensors: dict[str, bool] = dict.fromkeys(HA_BINARY_SENSORS, False)
        self._water_valve: bool = False
        self._pump_switch: bool = False
        self._washer_power: bool = False
        self._dryer_power: bool = False
        self._laundry_outlet: bool = False
        self._home_recliner: bool = False
        self._home_garage: bool = False

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
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._start_time: float = 0

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
        if hasattr(self, "_start_time"):
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

    def _get_state(self, entity_id: str) -> str | None:
        """Get entity state from HA"""
        try:
            response = self._session.get(
                f"{HA_URL}/api/states/{entity_id}",
                timeout=(3, HA_TIMEOUT),  # (connect_timeout, read_timeout)
            )
            if response.status_code == 200:
                return response.json().get("state")
        except (requests.exceptions.RequestException, ValueError):
            pass
        return None

    def _parse_numeric(self, value: str, default: Any = 0) -> Any:
        """Parse numeric value, handle 'unavailable', 'unknown', etc."""
        if value in (None, "unavailable", "unknown", "None", ""):
            return default
        try:
            s = str(value).strip()
            m = re.match(r"^([+-]?\d+\.?\d*)", s)
            if m:
                num = float(m.group(1))
                return int(num) if num == int(num) else num
            # Fallback: try direct conversion
            if "." in s:
                return float(s)
            return int(s)
        except Exception:
            return default

    def _parse_duration(self, value: str) -> int:
        """Parse duration in HH:MM:SS or MM:SS format to minutes"""
        if value in (None, "unavailable", "unknown", "None", ""):
            return 0
        try:
            # Try numeric first
            return int(float(value))
        except Exception:
            pass
        try:
            # Try HH:MM:SS or MM:SS format
            parts = str(value).split(":")
            if len(parts) == 3:
                hours, mins, secs = int(parts[0]), int(parts[1]), int(parts[2])
                return hours * 60 + mins + (1 if secs >= 30 else 0)
            if len(parts) == 2:
                mins, secs = int(parts[0]), int(parts[1])
                return mins + (1 if secs >= 30 else 0)
        except Exception:
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
                    logger.warning(
                        f"HA circuit breaker OPEN after {self._consecutive_failures} failures"
                    )

                # Throttle error logging to once per minute
                if now - self._last_error_log > 60:
                    logger.warning(f"HA poll failed ({self._consecutive_failures}x): {e}")
                    self._last_error_log = now

            time.sleep(HA_POLL_INTERVAL)

    def _poll_all(self):
        """Poll all entities from HA"""
        data = self._fetch_template_data()

        with self._lock:
            self._parse_sensors(data)
            self._parse_boolean_sensors(data)
            self._parse_switches(data)

    def _fetch_template_data(self) -> dict:
        """Fetch all entity data via template API"""
        template = self._build_template()

        try:
            response = self._session.post(
                f"{HA_URL}/api/template",
                json={"template": template},
                timeout=(3, HA_TIMEOUT),  # (connect_timeout, read_timeout)
            )
        except requests.exceptions.Timeout as exc:
            raise HomeAssistantTimeoutError("HA timeout") from exc
        except requests.exceptions.ConnectionError as exc:
            raise HomeAssistantConnectionError("HA connection failed") from exc

        if response.status_code != 200:
            raise HomeAssistantAPIError(f"HA API error: {response.status_code}")

        data = response.json()
        if not isinstance(data, dict):
            raise HomeAssistantResponseError("Invalid response format")

        return data

    def _parse_sensors(self, data: dict):
        """Parse numeric and duration sensors"""
        duration_sensors = {"dishwasher_duration", "washer_time", "dryer_time"}

        for key in HA_SENSORS:
            if key in data:
                self._sensors[key] = (
                    data[key] if key in duration_sensors else self._parse_numeric(data[key])
                )

        for key in VUE_SENSORS:
            if key in data:
                self._vue_sensors[key] = self._parse_numeric(data[key])

    def _parse_boolean_sensors(self, data: dict):
        """Parse boolean and binary sensor states"""
        for key in HA_BOOLEANS:
            if key in data:
                self._booleans[key] = data[key] == "on"

        for key in HA_BINARY_SENSORS:
            if key in data:
                self._binary_sensors[key] = data[key] == "on"

    def _parse_switches(self, data: dict):
        """Parse switch states into attributes"""
        switch_map = {
            "water_valve": "_water_valve",
            "pump_switch": "_pump_switch",
            "washer_power": "_washer_power",
            "dryer_power": "_dryer_power",
            "laundry_outlet": "_laundry_outlet",
            "home_recliner": "_home_recliner",
            "home_garage": "_home_garage",
        }

        for key, attr in switch_map.items():
            if key in data:
                setattr(self, attr, data[key] == "on")

    def _build_template(self) -> str:
        """Build Jinja2 template for batch fetch"""
        skip_sensors = {
            k
            for enabled, k in [
                (ENABLE_DISHWASHER, "dishwasher_duration"),
                (ENABLE_WASHER, "washer_time"),
                (ENABLE_DRYER, "dryer_time"),
                (ENABLE_WATER, "water_level"),
            ]
            if not enabled
        }
        skip_binary = {
            k
            for enabled, k in [
                (ENABLE_DISHWASHER, "dishwasher_running"),
            ]
            if not enabled
        }

        items = []

        for key, entity in HA_SENSORS.items():
            if key not in skip_sensors:
                items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')

        for key, entity in VUE_SENSORS.items():
            items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')

        for key, entity in HA_BOOLEANS.items():
            items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')

        for key, entity in HA_BINARY_SENSORS.items():
            if key not in skip_binary:
                items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')

        conditional_switches = [
            (ENABLE_WATER, HA_WATER_VALVE, "water_valve"),
            (ENABLE_WATER, HA_PUMP_SWITCH, "pump_switch"),
            (ENABLE_WASHER and HA_WASHER_POWER, HA_WASHER_POWER, "washer_power"),
            (ENABLE_DRYER and HA_DRYER_POWER, HA_DRYER_POWER, "dryer_power"),
            (
                (ENABLE_WASHER or ENABLE_DRYER) and HA_LAUNDRY_OUTLET,
                HA_LAUNDRY_OUTLET,
                "laundry_outlet",
            ),
        ]
        for condition, entity, key in conditional_switches:
            if condition:
                items.append(f'  "{key}": "{{{{ states("{entity}") }}}}"')

        items.append('  "home_recliner": "{{ states(\'switch.recliner_recliner\') }}"')
        items.append('  "home_garage": "{{ states(\'switch.garage_opener_l\') }}"')

        return "{\n" + ",\n".join(items) + "\n}"

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

    def get_all_vue_sensors(self) -> dict[str, Any]:
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

    @property
    def laundry_outlet_on(self) -> bool:
        with self._lock:
            return self._laundry_outlet

    @property
    def home_recliner_on(self) -> bool:
        with self._lock:
            return self._home_recliner

    @property
    def home_garage_on(self) -> bool:
        with self._lock:
            return self._home_garage

    def get_all_sensors(self) -> dict[str, Any]:
        """Get copy of all sensor values"""
        with self._lock:
            return dict(self._sensors)

    def get_all_booleans(self) -> dict[str, bool]:
        """Get copy of all boolean values"""
        with self._lock:
            return dict(self._booleans)

    # === Control Methods ===

    def _call_service(self, domain: str, action: str, entity_id: str) -> bool:
        """Call a HA service domain/action for an entity"""
        try:
            response = self._session.post(
                f"{HA_URL}/api/services/{domain}/{action}",
                json={"entity_id": entity_id},
                timeout=(3, HA_TIMEOUT),
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"{action} {entity_id} failed: {e}")
            return False

    def toggle_entity(self, entity_id: str) -> bool:
        """Toggle a switch or input_boolean"""
        return self._call_service(entity_id.split(".")[0], "toggle", entity_id)

    def press_button(self, entity_id: str) -> bool:
        """Press a button entity"""
        return self._call_service(entity_id.split(".")[0], "press", entity_id)

    def turn_on(self, entity_id: str) -> bool:
        """Turn on a switch or light"""
        return self._call_service(entity_id.split(".")[0], "turn_on", entity_id)

    def turn_off(self, entity_id: str) -> bool:
        """Turn off a switch or light"""
        return self._call_service(entity_id.split(".")[0], "turn_off", entity_id)

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
_ha_client: HomeAssistantClient | None = None


def get_ha() -> HomeAssistantClient:
    """Get or create HA client"""
    global _ha_client  # pylint: disable=global-statement
    if _ha_client is None:
        _ha_client = HomeAssistantClient()
        _ha_client.start()
    return _ha_client

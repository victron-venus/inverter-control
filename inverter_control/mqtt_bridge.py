#!/usr/bin/env python3
"""
MQTT Bridge for Inverter Control
Publishes state and subscribes to commands from remote dashboard
"""

import json
import logging
import math
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("inverter-control")


class SafeEncoder(json.JSONEncoder):
    """JSON encoder that converts NaN/Inf to null instead of invalid literals."""
    def default(self, obj):
        return super().default(obj)

    def encode(self, obj):
        return super().encode(self._sanitize(obj))

    def _sanitize(self, obj):
        if isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                return None
            return obj
        if isinstance(obj, dict):
            return {k: self._sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._sanitize(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self._sanitize(v) for v in obj)
        return obj

# Try to import paho-mqtt (may not be available on Venus OS)
try:
    import paho.mqtt.client as mqtt

    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False
    logger.info("paho-mqtt not available, MQTT bridge disabled")


class MQTTBridge:
    """Publishes state to MQTT, receives commands"""

    def __init__(self, broker: str = "localhost", port: int = 1883, prefix: str = "inverter"):
        self.broker = broker
        self.port = port
        self.prefix = prefix
        self._client: mqtt.Client | None = None
        self._connected = False
        self._callbacks: dict[str, Callable] = {}

        if not MQTT_AVAILABLE:
            return

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def connect(self):
        """Connect to MQTT broker"""
        if not self._client:
            return False

        try:
            self._client.connect_async(self.broker, self.port, 60)
            self._client.loop_start()
            logger.info(f"MQTT connecting to {self.broker}:{self.port}")
            return True
        except Exception as e:
            logger.warning(f"MQTT connection failed: {e}")
            return False

    def disconnect(self):
        """Disconnect from MQTT broker"""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc, properties=None):  # pylint: disable=too-many-arguments,unused-argument
        """Connected to broker"""
        self._connected = True
        logger.info("MQTT connected")

        # Subscribe to command topics
        client.subscribe(f"{self.prefix}/cmd/#")

    def _on_disconnect(self, client, userdata, rc, properties=None, reason_code=None):  # pylint: disable=too-many-arguments,unused-argument
        """Disconnected from broker"""
        self._connected = False
        if rc != 0:
            logger.warning(f"MQTT disconnected unexpectedly (rc={rc})")

    def _on_message(self, client, userdata, msg):
        """Received message"""
        try:
            topic = msg.topic
            cmd = topic.split("/")[-1]  # e.g. "inverter/cmd/toggle" -> "toggle"

            payload = {}
            if msg.payload:
                try:
                    payload = json.loads(msg.payload.decode())
                except json.JSONDecodeError:
                    payload = {"value": msg.payload.decode()}

            # Call registered callback
            if cmd in self._callbacks:
                self._callbacks[cmd](payload)
            else:
                logger.debug(f"Unknown command: {cmd}")

        except Exception as e:
            logger.exception(f"MQTT message error: {e}")

    def register_callback(self, command: str, callback: Callable[[dict], None]):
        """Register callback for command"""
        self._callbacks[command] = callback

    def publish_state(self, state: dict[str, Any]):
        """Publish current state"""
        if not self._client or not self._connected:
            return

        try:
            self._client.publish(f"{self.prefix}/state", json.dumps(state, cls=SafeEncoder), qos=0, retain=True)
        except Exception as e:
            logger.debug(f"MQTT publish error: {e}")

    def publish_console(self, line: str):
        """Publish console line"""
        if not self._client or not self._connected:
            return

        try:
            self._client.publish(f"{self.prefix}/console", line, qos=0)
        except Exception as e:
            logger.debug(f"MQTT console publish error: {e}")

    @property
    def connected(self) -> bool:
        return self._connected


# Global instance
_mqtt_bridge: MQTTBridge | None = None


def get_mqtt_bridge(
    broker: str = "localhost", port: int = 1883, prefix: str = "inverter"
) -> MQTTBridge | None:
    """Get or create MQTT bridge"""
    global _mqtt_bridge  # pylint: disable=global-statement

    if not MQTT_AVAILABLE:
        return None

    if _mqtt_bridge is None:
        _mqtt_bridge = MQTTBridge(broker, port, prefix)

    return _mqtt_bridge

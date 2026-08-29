#!/usr/bin/env python3
"""
MQTT Bridge for Inverter Control
Publishes state and subscribes to commands from remote dashboard
"""

import json
import logging
import math
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from .alert_state import get_alert_storage

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
    """Publishes state to MQTT, receives commands - async via background thread"""

    def __init__(self, broker: str = "localhost", port: int = 1883, prefix: str = "inverter"):
        self.broker = broker
        self.port = port
        self.prefix = prefix
        self._client: mqtt.Client | None = None
        self._connected = False
        self._callbacks: dict[str, Callable] = {}

        # Async publish queue
        self._publish_queue: queue.Queue[tuple[str, str, int, bool]] = queue.Queue(maxsize=100)
        self._publish_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Alert storage for persistence
        self._alert_storage = get_alert_storage()

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
            # Start async publish thread
            self._stop_event.clear()
            self._publish_thread = threading.Thread(
                target=self._publish_loop, daemon=True, name="MQTTPublish"
            )
            self._publish_thread.start()
            logger.info(f"MQTT connecting to {self.broker}:{self.port}")
            return True
        except Exception as e:
            logger.warning(f"MQTT connection failed: {e}")
            return False

    def disconnect(self):
        """Disconnect from MQTT broker"""
        self._stop_event.set()
        if self._publish_thread and self._publish_thread.is_alive():
            self._publish_thread.join(timeout=1.0)
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    def _publish_loop(self):
        """Background thread to drain publish queue"""
        while not self._stop_event.is_set():
            try:
                topic, payload, qos, retain = self._publish_queue.get(timeout=0.1)
                if self._client and self._connected:
                    self._client.publish(topic, payload, qos=qos, retain=retain)
                self._publish_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.debug(f"MQTT publish loop error: {e}")

    def _on_connect(self, client, userdata, flags, rc, properties=None):  # pylint: disable=too-many-arguments,unused-argument
        """Connected to broker"""
        self._connected = True
        logger.info("MQTT connected")

        # Subscribe to command topics
        client.subscribe(f"{self.prefix}/cmd/#")
        # Subscribe to alert acknowledgments
        client.subscribe(f"{self.prefix}/alert/ack")
        # Subscribe to solar forecast
        client.subscribe("solar/forecast")
        self._publish_portal_id(client)
        # Resend any unacknowledged alerts on (re)connection
        self.resend_unacknowledged_alerts()

    def _publish_portal_id(self, client) -> None:
        """Publish the VRM portal ID (retained) so remote consumers (desktop,
        dashboards) can discover the N/<portal>/... water topics without
        manual configuration."""
        try:
            from .config import PORTAL_ID  # lazy: first access runs detection

            if PORTAL_ID and PORTAL_ID != "your_portal_id":
                client.publish(f"{self.prefix}/portal", PORTAL_ID, qos=0, retain=True)
                logger.info("Published portal ID to %s/portal", self.prefix)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Portal ID publish failed: %s", e)

    def _on_disconnect(self, client, userdata, rc, properties=None, reason_code=None):  # pylint: disable=too-many-arguments,unused-argument
        """Disconnected from broker"""
        self._connected = False
        if rc != 0:
            logger.warning(f"MQTT disconnected unexpectedly (rc={rc})")

    def _on_message(self, client, userdata, msg):
        """Received message"""
        try:
            topic = msg.topic

            if topic == "solar/forecast":
                self._handle_forecast(msg.payload)
                return

            if topic == f"{self.prefix}/alert/ack":
                self._handle_acknowledgment(msg.payload.decode().strip())
                return

            cmd = topic.split("/")[-1]  # e.g. "inverter/cmd/toggle" -> "toggle"
            payload = self._parse_payload(msg.payload)
            if cmd in self._callbacks:
                self._callbacks[cmd](payload)
            else:
                logger.debug(f"Unknown command: {cmd}")

        except Exception as e:
            logger.exception(f"MQTT message error: {e}")

    @staticmethod
    def _parse_payload(raw: bytes | bytearray | None) -> dict:
        """Decode MQTT payload to dict; fall back to {"value": text} on bad JSON."""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            return {"value": raw.decode()}

    def _handle_forecast(self, payload: bytes | bytearray | None) -> None:
        """Dispatch a solar/forecast payload to the registered callback."""
        if not payload:
            return
        try:
            data = json.loads(payload.decode())
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in forecast message: {payload.decode()}")
            return
        callback = self._callbacks.get("forecast")
        if callback:
            callback(data)
        else:
            logger.debug("No forecast callback registered")

    def register_callback(self, command: str, callback: Callable[[dict], None]):
        """Register callback for command"""
        self._callbacks[command] = callback

    def _ensure_publish_thread(self):
        """Start publish thread if not running (lazy init for tests)"""
        if self._publish_thread is None or not self._publish_thread.is_alive():
            self._stop_event.clear()
            self._publish_thread = threading.Thread(
                target=self._publish_loop, daemon=True, name="MQTTPublish"
            )
            self._publish_thread.start()

    def publish_state(self, state: dict[str, Any]):
        """Publish current state (async, non-blocking)"""
        if not self._connected:
            return

        try:
            payload = json.dumps(state, cls=SafeEncoder)
            self._ensure_publish_thread()
            self._publish_queue.put_nowait((f"{self.prefix}/state", payload, 0, True))
        except queue.Full:
            logger.debug("MQTT publish queue full, dropping state update")
        except Exception as e:
            logger.debug(f"MQTT publish queue error: {e}")

    def publish_console(self, line: str):
        """Publish console line (async, non-blocking)"""
        if not self._connected:
            return

        try:
            self._ensure_publish_thread()
            self._publish_queue.put_nowait((f"{self.prefix}/console", line, 0, False))
        except queue.Full:
            pass  # Drop console lines silently when queue full
        except Exception as e:
            logger.debug(f"MQTT console queue error: {e}")

    def publish_notification(
        self,
        notification_id: str,
        level: Literal["info", "warning", "alarm"],
        title: str,
        body: str = "",
        source: str = "inverter-control",
    ):
        """Publish user-facing notification to {prefix}/notifications (async, non-blocking).

        Consumed by inverter-desktop banner; deduplicated by consumers on notification_id.
        Also stores the alert persistently for history and acknowledgment.
        """
        if not self._connected:
            logger.debug("MQTT not connected, dropping notification %s", notification_id)
            return

        # Store the alert persistently
        alert = self._alert_storage.add_alert(
            title=title,
            body=body,
            level=level,
            source=source,
        )
        # Use the persistent alert's ID for the notification (overrides the passed notification_id)
        notification_id = alert.id

        payload = json.dumps(
            {
                "id": notification_id,
                "level": level,
                "title": title,
                "body": body,
                "source": source,
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        try:
            self._ensure_publish_thread()
            self._publish_queue.put_nowait((f"{self.prefix}/notifications", payload, 0, False))
        except queue.Full:
            logger.debug("MQTT publish queue full, dropping notification %s", notification_id)
        except Exception as e:
            logger.debug(f"MQTT notification publish error: {e}")

    def _handle_acknowledgment(self, alert_id: str) -> None:
        """Handle acknowledgment from inverter-desktop"""
        if not alert_id:
            logger.debug("Received empty alert acknowledgment")
            return
        if self._alert_storage.acknowledge_alert(alert_id):
            logger.info("Alert %s acknowledged by inverter-desktop", alert_id)
        else:
            logger.warning("Received acknowledgment for unknown alert ID: %s", alert_id)

    def resend_unacknowledged_alerts(self) -> None:
        """Resend all unacknowledged alerts (called on (re)connection)"""
        alerts = self._alert_storage.get_unacknowledged_alerts()
        for alert in alerts:
            # Re-publish the alert by constructing the payload directly
            # to avoid double storage (since the alert is already stored)
            try:
                payload = json.dumps(
                    {
                        "id": alert.id,
                        "level": alert.level,
                        "title": alert.title,
                        "body": alert.body,
                        "source": alert.source,
                        "ts": alert.timestamp,  # Use the original timestamp
                    }
                )
                self._ensure_publish_thread()
                self._publish_queue.put_nowait((f"{self.prefix}/notifications", payload, 0, False))
                logger.info("Resent unacknowledged alert: %s", alert.id)
            except queue.Full:
                logger.debug("MQTT publish queue full, dropping alert %s", alert.id)
            except Exception as e:
                logger.debug(f"MQTT alert resend error: {e}")

    def flush(self):
        """Wait for publish queue to empty (for testing)"""
        self._publish_queue.join()

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

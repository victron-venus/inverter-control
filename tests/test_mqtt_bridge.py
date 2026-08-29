"""
Unit tests for MQTT Bridge
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from inverter_control import mqtt_bridge


class TestMQTTBridge:
    """Test MQTT bridge functionality"""

    def setup_method(self):
        """Reset global instance"""
        mqtt_bridge._mqtt_bridge = None

    def teardown_method(self):
        """Reset global instance"""
        mqtt_bridge._mqtt_bridge = None

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_init_mqtt_available(self, mock_mqtt):
        """Test initialization when MQTT is available"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(broker="test.broker", port=1883, prefix="test")

        assert bridge.broker == "test.broker"
        assert bridge.port == 1883
        assert bridge.prefix == "test"
        assert bridge._client == mock_client
        mock_client.on_connect = bridge._on_connect
        mock_client.on_message = bridge._on_message
        mock_client.on_disconnect = bridge._on_disconnect

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", False)
    def test_init_mqtt_unavailable(self):
        """Test initialization when MQTT is not available"""
        bridge = mqtt_bridge.MQTTBridge()

        assert bridge._client is None

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_connect_success(self, mock_mqtt):
        """Test successful connection"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        result = bridge.connect()

        assert result is True
        mock_client.connect_async.assert_called_once_with("localhost", 1883, 60)
        mock_client.loop_start.assert_called_once()

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_connect_failure(self, mock_mqtt):
        """Test connection failure"""
        mock_client = MagicMock()
        mock_client.connect_async.side_effect = Exception("Connection refused")
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        result = bridge.connect()

        assert result is False

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_disconnect(self, mock_mqtt):
        """Test disconnection"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        bridge.disconnect()

        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_connect(self, mock_mqtt):
        """Test on_connect callback"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        with patch("inverter_control.config.PORTAL_ID", "portal123"):
            bridge._on_connect(mock_client, None, None, 0)

        assert bridge._connected is True
        # Should subscribe to command topics and alert acknowledgments
        mock_client.subscribe.assert_any_call("test/cmd/#")
        mock_client.subscribe.assert_any_call("test/alert/ack")
        mock_client.subscribe.assert_any_call("solar/forecast")
        assert mock_client.subscribe.call_count == 3
        mock_client.publish.assert_called_once_with("test/portal", "portal123", qos=0, retain=True)

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_connect_skips_stub_portal(self, mock_mqtt):
        """Stub portal id (non-Venus dev machine) must not be published"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        with patch("inverter_control.config.PORTAL_ID", "your_portal_id"):
            bridge._on_connect(mock_client, None, None, 0)

        mock_client.publish.assert_not_called()

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_disconnect(self, mock_mqtt):
        """Test on_disconnect callback"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        bridge._connected = True
        bridge._on_disconnect(mock_client, None, 0)

        assert bridge._connected is False

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_message_with_json(self, mock_mqtt):
        """Test receiving JSON message"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        callback = MagicMock()
        bridge.register_callback("toggle", callback)

        mock_msg = MagicMock()
        mock_msg.topic = "test/cmd/toggle"
        mock_msg.payload = b'{"entity": "switch.test"}'

        bridge._on_message(mock_client, None, mock_msg)

        callback.assert_called_once_with({"entity": "switch.test"})

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_message_without_json(self, mock_mqtt):
        """Test receiving non-JSON message"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        callback = MagicMock()
        bridge.register_callback("press", callback)

        mock_msg = MagicMock()
        mock_msg.topic = "test/cmd/press"
        mock_msg.payload = b"raw_value"

        bridge._on_message(mock_client, None, mock_msg)

        callback.assert_called_once_with({"value": "raw_value"})

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_message_solar_forecast(self, mock_mqtt):
        """WIP solar/forecast subscription dispatches the forecast callback."""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        callback = MagicMock()
        bridge.register_callback("forecast", callback)

        mock_msg = MagicMock()
        mock_msg.topic = "solar/forecast"
        mock_msg.payload = b'{"today_kwh": 12.5}'

        bridge._on_message(mock_client, None, mock_msg)
        callback.assert_called_once_with({"today_kwh": 12.5})

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_message_unknown_command(self, mock_mqtt):
        """Test receiving unknown command"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        mock_msg = MagicMock()
        mock_msg.topic = "test/cmd/unknown"
        mock_msg.payload = b"{}"

        bridge._on_message(mock_client, None, mock_msg)

        # Should not raise, just log debug

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_register_callback(self, mock_mqtt):
        """Test registering command callback"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        callback = MagicMock()
        bridge.register_callback("setpoint", callback)

        assert bridge._callbacks["setpoint"] == callback

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_publish_state(self, mock_mqtt):
        """Test publishing state"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        bridge._connected = True

        state = {"gt": 100, "setpoint": 500}
        bridge.publish_state(state)
        # Flush the async queue
        bridge.flush()

        mock_client.publish.assert_called_once()
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == "test/state"
        import json

        assert json.loads(call_args[0][1]) == state
        assert call_args[1]["qos"] == 0
        assert call_args[1]["retain"] is True

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_publish_state_not_connected(self, mock_mqtt):
        """Test publishing state when not connected"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        bridge._connected = False

        bridge.publish_state({"test": "value"})

        mock_client.publish.assert_not_called()

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_publish_console(self, mock_mqtt):
        """Test publishing console line"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        bridge._connected = True

        bridge.publish_console("test line")
        # Flush the async queue
        bridge.flush()

        mock_client.publish.assert_called_once_with(
            "test/console", "test line", qos=0, retain=False
        )


class TestGetMqttBridge:
    """Test get_mqtt_bridge function"""

    def setup_method(self):
        mqtt_bridge._mqtt_bridge = None

    def teardown_method(self):
        mqtt_bridge._mqtt_bridge = None

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_get_mqtt_bridge_creates_instance(self, mock_mqtt):
        """Test get_mqtt_bridge creates new instance"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.get_mqtt_bridge("broker1", 1883, "prefix1")

        assert bridge is not None
        assert bridge.broker == "broker1"

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_get_mqtt_bridge_returns_singleton(self, mock_mqtt):
        """Test get_mqtt_bridge returns same instance"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge1 = mqtt_bridge.get_mqtt_bridge("broker1", 1883, "prefix1")
        bridge2 = mqtt_bridge.get_mqtt_bridge("broker2", 1883, "prefix2")

        assert bridge1 is bridge2

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", False)
    def test_get_mqtt_bridge_unavailable(self):
        """Test get_mqtt_bridge returns None when MQTT unavailable"""
        bridge = mqtt_bridge.get_mqtt_bridge()
        assert bridge is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

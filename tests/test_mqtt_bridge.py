"""
Unit tests for MQTT Bridge
"""

import json
import os
import sys
from types import SimpleNamespace
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
        bridge._connected = True
        bridge.disconnect()

        assert bridge.connected is False
        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_connect(self, mock_mqtt):
        """Test on_connect callback"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        with patch("inverter_control.config.PORTAL_ID", "portal123"):
            bridge = mqtt_bridge.MQTTBridge(prefix="test")
            bridge._on_connect(mock_client, None, None, 0)

        assert bridge._connected is True
        # Should subscribe to command topics and alert acknowledgments
        mock_client.subscribe.assert_any_call("test/cmd/#")
        mock_client.subscribe.assert_any_call("test/alert/ack")
        mock_client.subscribe.assert_any_call("solar/forecast")
        mock_client.subscribe.assert_any_call("solar_forecast/portal123/forecast_json", qos=1)
        mock_client.subscribe.assert_any_call("solar_forecast/portal123/pre_charge_request", qos=1)
        assert mock_client.subscribe.call_count == 5
        mock_client.publish.assert_any_call("test/portal", "portal123", qos=0, retain=True)
        assert mock_client.publish.call_count == 2
        topic, payload = mock_client.publish.call_args.args
        assert topic == "test/setpoint_override"
        assert json.loads(payload) == {"value": None, "last_error": None, "request_id": None}
        assert mock_client.publish.call_args.kwargs == {"qos": 1, "retain": True}

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_connect_skips_stub_portal(self, mock_mqtt):
        """Stub portal id (non-Venus dev machine) must not be published"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        with patch("inverter_control.config.PORTAL_ID", "your_portal_id"):
            bridge._on_connect(mock_client, None, None, 0)

        mock_client.publish.assert_called_once()
        assert mock_client.publish.call_args.args[0] == "test/setpoint_override"

    @pytest.mark.parametrize("connection_attempt", [1, 2])
    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_connect_replays_only_current_unacknowledged_alerts(
        self, mock_mqtt, isolated_alert_storage, connection_attempt
    ):
        """Each test starts empty, while reconnect still replays persisted alerts."""
        from inverter_control.alert_state import AlertStorage

        assert isolated_alert_storage.get_alert_history() == []
        pending = isolated_alert_storage.add_alert(f"pending {connection_attempt}", "body", "info")
        acknowledged = isolated_alert_storage.add_alert("acknowledged", "body", "info")
        isolated_alert_storage.acknowledge_alert(acknowledged.id)

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        # Reload from disk to exercise persistence as well as the replay path.
        bridge._alert_storage = AlertStorage(isolated_alert_storage.storage_path)
        mock_client = mock_mqtt.Client.return_value
        try:
            with patch("inverter_control.config.PORTAL_ID", "your_portal_id"):
                bridge._on_connect(mock_client, None, None, 0)
            bridge.flush()
            notifications = [
                item
                for item in mock_client.publish.call_args_list
                if item.args[0] == "test/notifications"
            ]
            assert len(notifications) == 1
            args, kwargs = notifications[0]
            assert args[0] == "test/notifications"
            assert json.loads(args[1])["id"] == pending.id
            assert kwargs == {"qos": 0, "retain": False}
        finally:
            bridge.disconnect()

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_disconnect(self, mock_mqtt):
        """Test on_disconnect callback"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge()
        bridge._connected = True
        bridge._on_disconnect(mock_client, None, None, 0)

        assert bridge._connected is False

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_message_with_json(self, mock_mqtt):
        """Test receiving JSON message"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        callback = MagicMock()
        bridge.register_callback("toggle", callback)

        mock_msg = MagicMock()
        mock_msg.topic = "test/cmd/toggle"
        mock_msg.payload = b'{"entity": "switch.test"}'
        mock_msg.retain = False

        bridge._on_message(mock_client, None, mock_msg)

        callback.assert_called_once_with({"entity": "switch.test"})

    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_on_message_without_json(self, mock_mqtt):
        """Test receiving non-JSON message"""
        mock_client = MagicMock()
        mock_mqtt.Client.return_value = mock_client

        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        callback = MagicMock()
        bridge.register_callback("press", callback)

        mock_msg = MagicMock()
        mock_msg.topic = "test/cmd/press"
        mock_msg.payload = b"raw_value"
        mock_msg.retain = False

        bridge._on_message(mock_client, None, mock_msg)

        callback.assert_called_once_with({"value": "raw_value"})

    @pytest.mark.parametrize(
        "retained,size,accepted",
        [(False, 100_000, True), (False, 100_001, False), (True, 2, False)],
    )
    @patch("inverter_control.mqtt_bridge.MQTT_AVAILABLE", True)
    @patch("inverter_control.mqtt_bridge.mqtt")
    def test_tariff_command_rejects_retained_and_oversized_before_parsing(
        self, mock_mqtt, retained, size, accepted
    ):
        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        callback = MagicMock()
        bridge.register_callback("electricity_tariff", callback)
        message = MagicMock(
            topic="test/cmd/electricity_tariff", payload=b"{}" + b" " * (size - 2), retain=retained
        )
        with patch.object(bridge, "_parse_payload", wraps=bridge._parse_payload) as parse:
            bridge._on_message(mock_mqtt.Client.return_value, None, message)
        if accepted:
            parse.assert_called_once()
            callback.assert_called_once_with({})
        else:
            parse.assert_not_called()
            callback.assert_not_called()

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


@pytest.fixture
def mocked_bridge():
    """Exercise real dispatch and persistence without connecting to a broker."""
    with patch("inverter_control.mqtt_bridge.mqtt.Client") as factory:
        bridge = mqtt_bridge.MQTTBridge(prefix="test")
        yield bridge, factory.return_value
        bridge.disconnect()


@pytest.mark.parametrize(
    "command",
    [
        "toggle",
        "press",
        "setpoint",
        "setpoint_override",
        "dry_run",
        "limits",
        "ess_mode",
        "loop_interval",
        "electricity_tariff",
    ],
)
def test_reconnect_never_replays_retained_control_commands(mocked_bridge, command):
    bridge, client = mocked_bridge
    callback = MagicMock()
    bridge.register_callback(command, callback)
    payload = {"entity": "charge_battery", "state": "on", "value": 2200}
    message = SimpleNamespace(
        topic=f"test/cmd/{command}", payload=json.dumps(payload).encode(), retain=True
    )
    for _ in range(2):
        bridge._on_connect(client, None, None, 0)
        bridge._on_message(client, None, message)
        bridge._on_disconnect(client, None, None, 0)
    callback.assert_not_called()

    message.retain = False
    bridge._on_connect(client, None, None, 0)
    bridge._on_message(client, None, message)
    callback.assert_called_once_with(payload)


@pytest.mark.parametrize("topic", ["other/cmd/toggle", "test/cmd/nested/toggle", "test/toggle"])
def test_command_requires_its_exact_namespace(mocked_bridge, topic):
    bridge, client = mocked_bridge
    callback = MagicMock()
    bridge.register_callback("toggle", callback)
    bridge._on_message(client, None, SimpleNamespace(topic=topic, payload=b"{}", retain=False))
    callback.assert_not_called()


def test_rejected_connack_does_not_publish_or_subscribe(mocked_bridge):
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    bridge, client = mocked_bridge
    bridge._connected = True
    with patch.object(bridge, "resend_unacknowledged_alerts") as resend:
        bridge._on_connect(client, None, None, ReasonCode(PacketTypes.CONNACK, "Not authorized"))
    assert bridge.connected is False
    client.subscribe.assert_not_called()
    client.publish.assert_not_called()
    resend.assert_not_called()


def test_v2_disconnect_logs_reason_not_flags(mocked_bridge, caplog):
    from paho.mqtt.client import DisconnectFlags
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    bridge, client = mocked_bridge
    bridge._connected = True
    bridge._on_disconnect(client, None, DisconnectFlags(False), ReasonCode(PacketTypes.DISCONNECT))
    assert bridge.connected is False
    assert "unexpectedly" not in caplog.text
    bridge._on_disconnect(
        client, None, DisconnectFlags(True), ReasonCode(PacketTypes.DISCONNECT, "Not authorized")
    )
    assert "Not authorized" in caplog.text


@pytest.mark.parametrize("failure", [None, "loop_stop", "disconnect"])
def test_connect_callback_finishing_during_shutdown_cannot_restore_connected(
    mocked_bridge, failure
):
    bridge, client = mocked_bridge

    def finish_callback():
        bridge._on_connect(client, None, None, 0)
        assert bridge.connected is True  # The pending callback really ran.
        if failure == "loop_stop":
            raise RuntimeError("network thread stop failed")

    client.loop_stop.side_effect = finish_callback
    if failure == "disconnect":
        client.disconnect.side_effect = RuntimeError("socket disconnect failed")
    try:
        if failure:
            with pytest.raises(RuntimeError):
                bridge.disconnect()
        else:
            bridge.disconnect()
        assert bridge.connected is False
        assert bridge._stop_event.is_set()
        client.publish.reset_mock()
        bridge.publish_state({"booleans": {"charge_battery": False}})
        client.publish.assert_not_called()
        assert bridge._publish_queue.empty()
    finally:
        client.loop_stop.side_effect = None
        client.disconnect.side_effect = None


def test_lost_precharge_ack_retry_and_restart_do_not_repeat_intent(mocked_bridge, tmp_path):
    from test_precharge_notification import request

    from inverter_control.precharge import PrechargeInbox

    bridge, client = mocked_bridge
    path = tmp_path / "precharge.json"
    inbox = PrechargeInbox(path)
    accepted = MagicMock()
    bridge.register_callback("pre_charge", lambda p: inbox.handle(p, lambda: False, accepted))
    message = SimpleNamespace(
        topic=f"{bridge.forecast_prefix}/pre_charge_request",
        payload=json.dumps(request()).encode(),
        retain=False,
    )
    client.publish.side_effect = OSError("connection lost before application ACK")
    bridge._on_message(client, None, message)
    accepted.assert_called_once()

    # Reconnect/restart reuses the durable inbox even when the sender never saw ACK.
    inbox = PrechargeInbox(path)
    client.publish.side_effect = None
    bridge._on_message(client, None, message)
    accepted.assert_called_once()
    topic, raw = client.publish.call_args.args
    ack = json.loads(raw)
    assert topic == f"{bridge.forecast_prefix}/pre_charge_ack/test-day"
    assert (ack["status"], ack["original_status"]) == ("duplicate", "accepted")
    assert client.publish.call_args.kwargs == {"qos": 1, "retain": False}

    message.retain = True
    client.publish.reset_mock()
    bridge._on_message(client, None, message)
    accepted.assert_called_once()
    client.publish.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

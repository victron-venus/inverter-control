"""Dropped state cannot refresh the daemon's local MQTT acceptance timestamp."""

import queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from inverter_control.mqtt_bridge import MQTTBridge


@pytest.fixture
def bridge():
    bridge = MQTTBridge.__new__(MQTTBridge)
    bridge.prefix = "inverter"
    bridge._connected = True
    bridge._publish_queue = queue.Queue(maxsize=100)
    bridge._ensure_publish_thread = Mock()
    bridge._client = Mock()
    return bridge


def test_accepted_state_is_not_a_delivery_acknowledgement(bridge):
    assert bridge.publish_state({"value": 3}) is True
    assert bridge._publish_queue.get_nowait() == ("inverter/state", '{"value": 3}', 0, True)
    bridge._client.publish.assert_not_called()


@pytest.mark.parametrize("reason", ["disconnected", "full", "encoding", "worker_start"])
def test_publish_state_returns_false_for_no_enqueue(bridge, reason):
    state = {"value": 3}
    if reason == "disconnected":
        bridge._connected = False
    elif reason == "full":
        for index in range(100):
            bridge._publish_queue.put_nowait(("inverter/console", str(index), 0, False))
    elif reason == "encoding":
        state = {"bad": object()}
    else:
        bridge._ensure_publish_thread.side_effect = RuntimeError("worker unavailable")
    before = list(bridge._publish_queue.queue)
    assert bridge.publish_state(state) is False
    assert list(bridge._publish_queue.queue) == before
    bridge._client.publish.assert_not_called()


@pytest.mark.parametrize("reason", ["accepted", "full", "encoding"])
def test_main_marks_only_real_state_enqueue_and_keeps_ack_console_order(bridge, reason):
    state = {"booleans": {"charge_battery": False}}
    if reason == "full":
        for index in range(100):
            bridge._publish_queue.put_nowait(("inverter/console", str(index), 0, False))
    if reason == "encoding":
        state = {"bad": object()}
    events = []
    actual_publish = bridge.publish_state

    def publish(value):
        events.append("state")
        return actual_publish(value)

    bridge.publish_state = publish
    bridge.publish_console = Mock(side_effect=lambda value: events.append("console"))
    watchdog = SimpleNamespace(
        publish_override_status=Mock(side_effect=lambda: events.append("override")),
        mark_mqtt_update=Mock(side_effect=lambda: events.append("accepted")),
    )
    controller = SimpleNamespace(
        get_state_for_mqtt=lambda: state,
        _watchdog=watchdog,
        last_console_line="latest console line",
    )
    main._publish_state(controller, bridge)
    if reason == "accepted":
        assert events == ["state", "override", "accepted", "console"]
        watchdog.mark_mqtt_update.assert_called_once_with()
        assert bridge._publish_queue.qsize() == 1
    else:
        assert events == ["state", "override", "console"]
        watchdog.mark_mqtt_update.assert_not_called()
        assert bridge._publish_queue.qsize() == (100 if reason == "full" else 0)
    bridge.publish_console.assert_called_once_with("latest console line")

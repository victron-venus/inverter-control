"""A retained status send cannot hold up control or reverse newer intent."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from inverter_control import mqtt_bridge
from inverter_control.watchdog import HardwareWatchdog


class ObservedSendLock:
    """Real mutex with a barrier proving the second sender has tried to enter."""

    def __init__(self):
        self.lock = threading.Lock()
        self.count_lock = threading.Lock()
        self.contending = threading.Event()
        self.calls = 0

    def __enter__(self):
        with self.count_lock:
            self.calls += 1
            if self.calls == 2:
                self.contending.set()
        self.lock.acquire()

    def __exit__(self, *_args):
        self.lock.release()


@pytest.mark.parametrize("first_sender", ["worker", "connect"])
def test_slow_override_send_does_not_block_control_and_latest_stop_stays_last(
    monkeypatch, first_sender
):
    client = Mock()
    monkeypatch.setattr(mqtt_bridge, "MQTT_AVAILABLE", True)
    monkeypatch.setattr(mqtt_bridge.mqtt, "Client", Mock(return_value=client))
    bridge = mqtt_bridge.MQTTBridge(prefix="test")
    send_lock = bridge._setpoint_override_publish_lock = ObservedSendLock()
    bridge._alert_storage = Mock(get_unacknowledged_alerts=Mock(return_value=[]))
    victron = Mock(set_grid_setpoint=Mock(return_value=True))
    watchdog = HardwareWatchdog(victron)
    watchdog.set_override_status_callback(bridge.publish_setpoint_override)
    # This is the actual daemon callback and hardware-lock ownership path.
    watchdog.set_setpoint_override(-520, "start")
    entered, release, control_done, stop_sent = (threading.Event() for _ in range(4))
    frames, errors, threads = [], [], []
    calls = 0

    def publish(topic, payload, *, qos, retain):
        nonlocal calls
        if topic != "test/setpoint_override":
            return
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
        frame = json.loads(payload)
        frames.append((frame, qos, retain))
        if frame["request_id"] == "stop":
            stop_sent.set()

    def run(call, done=None):
        def invoke():
            try:
                call()
            except Exception as error:
                errors.append(error)
            finally:
                if done is not None:
                    done.set()

        thread = threading.Thread(target=invoke)
        threads.append(thread)
        thread.start()

    client.publish.side_effect = publish
    controller = SimpleNamespace(
        _watchdog=watchdog,
        get_state_for_mqtt=lambda: {"fixture": "control"},
        last_console_line=None,
    )
    try:
        if first_sender == "worker":
            bridge._connected = True
            watchdog.publish_override_status()
        else:
            run(lambda: bridge._on_connect(client, None, None, 0))
        assert entered.wait(1)
        run(lambda: main._publish_state(controller, bridge), control_done)
        # The normal control loop must return while the transport is still blocked.
        assert control_done.wait(0.3), "status send blocked the production control callback"
        assert not release.is_set()
        stop_done = threading.Event()
        run(lambda: watchdog.set_setpoint_override(None, "stop"), stop_done)
        assert stop_done.wait(0.3), "in-flight Start send blocked the newer Stop producer"
        assert watchdog.get_setpoint_override()["value"] is None
        if first_sender == "worker":
            # Reconnect replay competes with the queued Stop; it must use the
            # same publication ordering as the worker, without blocking control.
            run(lambda: bridge._on_connect(client, None, None, 0))
        assert send_lock.contending.wait(1)
        assert calls == 1 and frames == []
        release.set()
        for thread in threads:
            thread.join(1)
        assert stop_sent.wait(1)
        bridge.flush()
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert frames[0][0]["request_id"] == "start"
        assert frames[-1][0] == {"value": None, "last_error": None, "request_id": "stop"}
        assert all(frame[0]["request_id"] == "stop" for frame in frames[1:])
        assert all((qos, retain) == (1, True) for _, qos, retain in frames)
        victron.set_grid_setpoint.assert_called_once_with(-520)
    finally:
        release.set()
        for thread in threads:
            thread.join(1)
        bridge.disconnect()


@pytest.mark.parametrize("send_fails", [False, True])
def test_reconnect_does_not_clear_newer_pending_stop_when_queue_is_full(monkeypatch, send_fails):
    client = Mock()
    monkeypatch.setattr(mqtt_bridge, "MQTT_AVAILABLE", True)
    monkeypatch.setattr(mqtt_bridge.mqtt, "Client", Mock(return_value=client))
    bridge = mqtt_bridge.MQTTBridge(prefix="test")
    bridge._ensure_publish_thread = Mock()  # Hold a bounded, full queue for the fixture.
    bridge._alert_storage = Mock(get_unacknowledged_alerts=Mock(return_value=[]))
    monkeypatch.setattr(mqtt_bridge.logger, "warning", Mock())
    bridge.publish_setpoint_override({"value": -520, "last_error": None, "request_id": "start"})
    for _ in range(bridge._publish_queue.maxsize):
        bridge._publish_queue.put_nowait(("test/state", "{}", 0, True))
    entered, release, stop_done = (threading.Event() for _ in range(3))
    errors = []
    stop_status = {"value": None, "last_error": None, "request_id": "stop"}

    def publish(topic, _payload, **_kwargs):
        if topic == "test/setpoint_override":
            entered.set()
            assert release.wait(3)
            if send_fails:
                raise OSError("fixture send failure")

    def reconnect():
        try:
            bridge._on_connect(client, None, None, 0)
        except Exception as error:
            errors.append(error)

    def stop():
        bridge.publish_setpoint_override(stop_status)
        stop_done.set()

    client.publish.side_effect = publish
    sender = threading.Thread(target=reconnect)
    producer = threading.Thread(target=stop)
    try:
        sender.start()
        assert entered.wait(1)
        producer.start()
        assert stop_done.wait(0.3)
        assert bridge._setpoint_override_pending is True
        release.set()
        sender.join(1)
        producer.join(1)
        assert not sender.is_alive() and not producer.is_alive()
        assert len(errors) == int(send_fails)
        if errors:
            assert isinstance(errors[0], OSError)
        assert bridge._setpoint_override_status == stop_status
        assert bridge._setpoint_override_pending is True
        # Free one slot. The unchanged latest status must retry its wakeup,
        # preserving local enqueue semantics rather than asserting delivery.
        bridge._publish_queue.get_nowait()
        bridge._publish_queue.task_done()
        bridge.publish_setpoint_override(stop_status)
        assert bridge._setpoint_override_pending is False
        assert bridge._publish_queue.qsize() == bridge._publish_queue.maxsize
        assert bridge._publish_queue.queue[-1] == ("test/setpoint_override", "", 1, True)
    finally:
        release.set()
        sender.join(1)
        if producer.ident is not None:
            producer.join(1)
        bridge.disconnect()

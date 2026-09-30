"""Offline regressions for queue completion and publisher ownership."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inverter_control import mqtt_bridge


@pytest.fixture
def bridge(monkeypatch):
    client = Mock()
    monkeypatch.setattr(mqtt_bridge, "MQTT_AVAILABLE", True)
    monkeypatch.setattr(mqtt_bridge.mqtt, "Client", Mock(return_value=client))
    instance = mqtt_bridge.MQTTBridge(prefix="test")
    instance._connected = True
    yield instance, client
    instance.disconnect()


@pytest.mark.parametrize("topic", ["test/state", "test/setpoint_override"])
def test_publish_exception_completes_local_queue_item_and_next_item(bridge, topic):
    instance, client = bridge
    calls = []

    def publish(received_topic, *_args, **_kwargs):
        calls.append(received_topic)
        if len(calls) == 1:
            raise OSError("mock socket write failed")
        instance._stop_event.set()

    client.publish.side_effect = publish
    instance._publish_queue.put_nowait((topic, "{}", 0, True))
    instance._publish_queue.put_nowait(("test/console", "next", 0, False))
    # Run the real worker body synchronously; the second item stops it.
    instance._publish_loop()
    assert calls == [topic, "test/console"]
    assert instance._publish_queue.empty()
    # flush() uses Queue.join(): a stranded unfinished item would wait forever.
    assert instance._publish_queue.unfinished_tasks == 0
    instance.flush()


def test_fast_connect_callback_and_connect_own_one_publish_worker(bridge, monkeypatch):
    instance, client = bridge
    instance._connected = False
    instance._alert_storage = Mock()
    instance._alert_storage.get_unacknowledged_alerts.return_value = [
        SimpleNamespace(
            id="pending", level="info", title="pending", body="", source="test", timestamp="now"
        )
    ]
    entered, release = threading.Event(), threading.Event()
    real_thread = threading.Thread
    workers = []

    def thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    def publish(topic, *_args, **_kwargs):
        if topic == "test/notifications":
            entered.set()
            assert release.wait(timeout=3)

    monkeypatch.setattr(mqtt_bridge.threading, "Thread", thread)
    client.publish.side_effect = publish
    # A real Paho callback may run before loop_start() returns to connect().
    client.loop_start.side_effect = lambda: instance._on_connect(client, None, None, 0)
    try:
        assert instance.connect() is True
        assert entered.wait(timeout=1)
        assert len(workers) == 1
        assert instance._publish_thread is workers[0]
        # A blocked publisher must stay tracked: shutdown must not report it stopped.
        assert instance.disconnect() is False
        assert workers[0].is_alive()
    finally:
        release.set()
        instance.request_stop()
        for worker in workers:
            worker.join(timeout=1)
        assert all(not worker.is_alive() for worker in workers)

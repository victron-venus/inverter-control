"""MQTT diagnostics cannot make a full local queue block hardware callbacks."""

import json
import logging
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from inverter_control import mqtt_bridge
from inverter_control.watchdog import HardwareWatchdog


@pytest.fixture
def isolated_log(monkeypatch):
    log = logging.getLogger("mqtt-backpressure-fixture")
    monkeypatch.setattr(log, "handlers", [])
    monkeypatch.setattr(log, "propagate", False)
    log.setLevel(logging.DEBUG)
    monkeypatch.setattr(mqtt_bridge, "logger", log)
    return log


@pytest.fixture
def bridge(monkeypatch):
    client = Mock()
    monkeypatch.setattr(mqtt_bridge, "MQTT_AVAILABLE", True)
    monkeypatch.setattr(mqtt_bridge.mqtt, "Client", Mock(return_value=client))
    bridge = mqtt_bridge.MQTTBridge(prefix="test")
    yield bridge
    bridge.disconnect()


class HeldHandler(logging.Handler):
    """Hold a real Handler's lock, just as a slow console/file sink does."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.records = []

    def emit(self, record):
        self.records.append((record.threadName, record.getMessage()))
        if record.getMessage() == "fixture hold":
            self.entered.set()
            assert self.unblock.wait(5)


@pytest.mark.parametrize("failure", ["override_full", "state_full", "state_encoding"])
def test_held_log_sink_cannot_block_main_callback_or_pending_stop(bridge, isolated_log, failure):
    client = bridge._client
    watchdog = HardwareWatchdog(Mock(set_grid_setpoint=Mock(return_value=True)))
    watchdog.set_override_status_callback(bridge.publish_setpoint_override)
    watchdog.set_setpoint_override(-520, "start")
    watchdog.mark_mqtt_update = Mock()
    sink = HeldHandler()
    log = isolated_log
    log.setLevel(logging.WARNING if failure == "override_full" else logging.DEBUG)
    log.addHandler(sink)
    blocked_send, release_send, control_done, stop_done, stop_sent = (
        threading.Event() for _ in range(5)
    )
    errors, threads, overrides = [], [], []

    def run(call, done=None):
        def invoke():
            try:
                call()
            except Exception as error:
                errors.append(error)
            finally:
                if done is not None:
                    done.set()

        thread = threading.Thread(target=invoke, name="FixtureProducer")
        threads.append(thread)
        thread.start()

    def publish(topic, payload, *, qos, retain):
        if payload == "held transport":
            blocked_send.set()
            assert release_send.wait(5)
        if topic == "test/setpoint_override":
            overrides.append((json.loads(payload), qos, retain))
            stop_sent.set()

    client.publish.side_effect = publish
    controller = SimpleNamespace(
        _watchdog=watchdog,
        get_state_for_mqtt=lambda: (
            {"bad": object()} if failure == "state_encoding" else {"fixture": "control"}
        ),
        last_console_line=None,
    )
    try:
        bridge._connected = True
        bridge.publish_console("held transport")
        assert blocked_send.wait(1)
        for index in range(100):
            bridge.publish_console(str(index))
        assert bridge._publish_queue.qsize() == bridge._publish_queue.maxsize == 100
        run(lambda: log.warning("fixture hold"))
        assert sink.entered.wait(1)
        run(lambda: main._publish_state(controller, bridge), control_done)
        assert control_done.wait(0.3), "held logging.Handler blocked main._publish_state"
        assert not release_send.is_set() and not sink.unblock.is_set()
        watchdog.mark_mqtt_update.assert_not_called()
        run(lambda: watchdog.set_setpoint_override(None, "stop"), stop_done)
        assert stop_done.wait(0.3), "held logging.Handler blocked the latest Stop callback"
        assert watchdog.get_setpoint_override()["request_id"] == "stop"
        assert bridge._setpoint_override_pending is True
        assert bridge._publish_queue.qsize() == 100
        # Release the independent fake transport and real sink, then retry the
        # unchanged pending Stop using the daemon's actual publication path.
        release_send.set()
        sink.unblock.set()
        drained = threading.Event()
        run(bridge.flush, drained)
        assert drained.wait(2)
        controller.get_state_for_mqtt = lambda: {"fixture": "recovered"}
        main._publish_state(controller, bridge)
        assert stop_sent.wait(1)
        watchdog.mark_mqtt_update.assert_called_once_with()
        assert bridge._setpoint_override_pending is False
        assert overrides[-1] == (
            {"value": None, "last_error": None, "request_id": "stop"},
            1,
            True,
        )
        assert not errors
    finally:
        release_send.set()
        sink.unblock.set()
        for thread in threads:
            thread.join(2)
        bridge.disconnect()
        assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("producer", ["state", "console"])
def test_enqueue_errors_keep_only_type_without_calling_exception_str(
    bridge, isolated_log, monkeypatch, producer
):
    class UnprintableError(RuntimeError):
        def __str__(self):
            raise AssertionError("producer must not stringify an arbitrary exception")

    bridge._connected = True
    original_start = bridge._ensure_publish_thread
    monkeypatch.setattr(bridge, "_ensure_publish_thread", Mock(side_effect=UnprintableError()))
    sink = HeldHandler()
    isolated_log.addHandler(sink)
    holder = threading.Thread(target=lambda: isolated_log.warning("fixture hold"))
    done, results, errors = threading.Event(), [], []

    def produce():
        try:
            if producer == "state":
                results.append(bridge.publish_state({"valid": True}))
            else:
                results.append(bridge.publish_console("fixture"))
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=produce)
    try:
        holder.start()
        assert sink.entered.wait(1)
        thread.start()
        assert done.wait(0.3)
        assert not errors
        assert results == ([False] if producer == "state" else [None])
        assert bridge._publish_queue.empty()
        assert len(bridge._publish_diagnostics) == 1
        assert bridge._publish_diagnostics[0][2] == "UnprintableError"
    finally:
        sink.unblock.set()
        holder.join(1)
        if thread.ident is not None:
            thread.join(1)
        monkeypatch.setattr(bridge, "_ensure_publish_thread", original_start)


def test_diagnostic_overflow_and_lock_contention_leave_pending_ack_intact(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_ensure_publish_thread", Mock())
    watchdog = HardwareWatchdog(Mock())
    watchdog.set_override_status_callback(bridge.publish_setpoint_override)
    bridge._connected = True
    for _ in range(100):
        bridge._publish_queue.put_nowait(("test/state", "{}", 0, True))
    queued = list(bridge._publish_queue.queue)
    for _ in range(1000):
        watchdog.publish_override_status()
    assert len(bridge._publish_diagnostics) == bridge._publish_diagnostics.maxlen == 32
    assert bridge._setpoint_override_pending is True
    diagnostics = list(bridge._publish_diagnostics)
    done = threading.Event()

    def stop():
        watchdog.set_setpoint_override(None, "latest-stop")
        done.set()

    producer = threading.Thread(target=stop)
    try:
        with bridge._publish_diagnostics_lock:
            producer.start()
            assert done.wait(0.3), "a diagnostic consumer lock blocked the hardware callback"
            assert list(bridge._publish_diagnostics) == diagnostics
        assert list(bridge._publish_queue.queue) == queued
        assert bridge._setpoint_override_status["request_id"] == "latest-stop"
        assert bridge._setpoint_override_pending is True
        bridge._publish_queue.get_nowait()
        bridge._publish_queue.task_done()
        watchdog.publish_override_status()
        assert bridge._setpoint_override_pending is False
        assert bridge._publish_queue.qsize() == 100
        assert bridge._publish_queue.queue[-1] == ("test/setpoint_override", "", 1, True)
    finally:
        producer.join(1)


def test_drain_is_outside_all_producer_locks_and_task_done_precedes_sink(bridge, isolated_log):
    sink = HeldHandler()
    isolated_log.addHandler(sink)
    bridge._connected = True
    bridge._record_publish_diagnostic(logging.WARNING, "fixture hold")
    try:
        assert bridge.publish_state({"value": 1}) is True
        assert sink.entered.wait(1)
        bridge._client.publish.assert_called_once_with(
            "test/state", '{"value": 1}', qos=0, retain=True
        )
        assert bridge._publish_queue.unfinished_tasks == 0
        assert sink.records == [("MQTTPublish", "fixture hold")]
        for lock in (
            bridge._setpoint_override_lock,
            bridge._setpoint_override_publish_lock,
            bridge._publish_diagnostics_lock,
        ):
            assert lock.acquire(blocking=False), "logging sink retained a producer lock"
            lock.release()
        worker = bridge._publish_thread
        assert bridge.disconnect() is False  # The blocked sink remains tracked for shutdown.
        assert bridge._publish_thread is worker and worker.is_alive()
    finally:
        sink.unblock.set()
        if bridge._publish_thread is not None:
            bridge._publish_thread.join(1)
    assert bridge.disconnect() is True


def test_raising_handler_cannot_kill_publisher_or_lose_queue_completion(bridge, isolated_log):
    emitted, second_sent = threading.Event(), threading.Event()

    class RaisingHandler(logging.Handler):
        def emit(self, record):
            emitted.set()
            raise OSError("fixture broken sink")

    isolated_log.addHandler(RaisingHandler())
    bridge._connected = True
    bridge._record_publish_diagnostic(logging.WARNING, "fixture diagnostic")
    assert bridge.publish_state({"value": 1}) is True
    assert emitted.wait(1)
    worker = bridge._publish_thread
    bridge._client.publish.side_effect = lambda *_a, **_kw: second_sent.set()
    assert bridge.publish_state({"value": 2}) is True
    assert second_sent.wait(1)
    assert bridge._publish_thread is worker and worker.is_alive()
    assert bridge.disconnect() is True
    assert bridge._publish_queue.unfinished_tasks == 0


def test_reentrant_console_logging_cannot_generate_endless_diagnostics(
    bridge, isolated_log, monkeypatch
):
    messages = []
    two_idle_polls = threading.Event()
    original_get = bridge._publish_queue.get
    empty_polls = 0

    def get(*args, **kwargs):
        nonlocal empty_polls
        try:
            return original_get(*args, **kwargs)
        except queue.Empty:
            empty_polls += 1
            if empty_polls >= 2:
                two_idle_polls.set()
            raise

    class ForwardingHandler(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())
            with monkeypatch.context() as context:
                context.setattr(bridge._publish_queue, "put_nowait", Mock(side_effect=OSError()))
                bridge.publish_console(record.getMessage())

    monkeypatch.setattr(bridge._publish_queue, "get", get)
    isolated_log.addHandler(ForwardingHandler())
    bridge._connected = True
    bridge._record_publish_diagnostic(logging.WARNING, "fixture diagnostic")
    assert bridge.publish_state({"value": 1}) is True
    assert two_idle_polls.wait(1)
    assert messages == ["fixture diagnostic"]
    assert not bridge._publish_diagnostics
    assert bridge._publish_thread.is_alive()
    assert bridge._publish_queue.unfinished_tasks == 0

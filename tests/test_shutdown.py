"""Real worker lifecycle under concurrent cancellation; no broker or device I/O."""

import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from inverter_control.background_reader import BackgroundReader
from inverter_control.controller import InverterController
from inverter_control.homeassistant import HomeAssistantClient
from inverter_control.mqtt_bridge import MQTTBridge
from inverter_control.watchdog import HardwareWatchdog


def bind_readers(readers):
    controller = SimpleNamespace(
        **dict(zip(("water", "evcharger", "telemetry", "performance"), readers, strict=True))
    )
    controller.request_stop_auxiliary_readers = MethodType(
        InverterController.request_stop_auxiliary_readers, controller
    )
    controller.stop_auxiliary_readers = MethodType(
        InverterController.stop_auxiliary_readers, controller
    )
    return controller


def test_slow_readers_all_receive_stop_before_first_join_and_share_budget():
    release = threading.Event()
    entered = [threading.Event() for _ in range(4)]
    readers = []
    for index in range(4):

        def refresh(i=index):
            entered[i].set()
            release.wait(2)
            return {"value": i}

        readers.append(BackgroundReader(refresh, {}, name=f"shutdown-fixture-{index}"))
        readers[-1].start()
    assert all(event.wait(1) for event in entered)
    controller = bind_readers(readers)
    budgets = []
    original_stop = readers[0].stop

    def first_stop(timeout):
        assert all(reader._stop.is_set() for reader in readers)
        budgets.append(timeout)
        return original_stop(timeout)

    readers[0].stop = first_stop
    started = time.monotonic()
    try:
        assert controller.stop_auxiliary_readers(timeout=0.08) is False
        assert time.monotonic() - started < 0.3  # Four serial waits would use0.32s.
        assert 0 <= budgets[0] <= 0.08
        assert all(reader.read() == {} for reader in readers)
    finally:
        release.set()
        for reader in readers:
            assert reader.stop(timeout=1)


def test_reader_join_cancellation_propagates_after_all_stop_requests():
    readers = [Mock() for _ in range(4)]
    readers[0].stop.side_effect = KeyboardInterrupt
    controller = bind_readers(readers)
    with pytest.raises(KeyboardInterrupt):
        controller.stop_auxiliary_readers(timeout=0.1)
    for reader in readers:
        reader.request_stop.assert_called_once()


def quiet_worker():
    return SimpleNamespace(request_stop=Mock(), stop=Mock(return_value=True))


def shutdown_context(monkeypatch, watchdog, bridge):
    closed = threading.Event()

    def close(**_kwargs):
        closed.set()
        return True

    controller = SimpleNamespace(
        _watchdog=watchdog,
        grid_filter=quiet_worker(),
        derived_grid_filter=quiet_worker(),
        request_stop_auxiliary_readers=Mock(),
        stop_auxiliary_readers=Mock(return_value=True),
        ha=quiet_worker(),
        victron=SimpleNamespace(
            request_stop=Mock(),
            stop_polling=Mock(return_value=True),
            close=Mock(side_effect=close),
        ),
    )
    heartbeat = SimpleNamespace(join=Mock(), is_alive=lambda: False)
    monkeypatch.setattr(main, "request_stop_console_server", Mock())
    monkeypatch.setattr(main, "stop_console_server", Mock(return_value=True))
    return controller, heartbeat, closed


@pytest.mark.parametrize("release_first", ["mqtt", "watchdog"])
def test_shared_io_waits_for_both_actual_watchdog_write_and_mqtt_callback(
    monkeypatch, release_first
):
    writer_entered, writer_release = threading.Event(), threading.Event()
    mqtt_entered, mqtt_release = threading.Event(), threading.Event()
    watcher_done = threading.Event()

    def physical_write(_watts):
        writer_entered.set()
        assert writer_release.wait(2)
        return True

    watchdog = HardwareWatchdog(
        SimpleNamespace(set_grid_setpoint=physical_write), check_interval=10
    )
    watchdog._override_value = 100
    watchdog.start()
    assert writer_entered.wait(1)

    # Public Paho loop_stop waits for a current callback; no Paho private state.
    class Client:
        def loop_stop(self):
            mqtt_entered.set()
            assert mqtt_release.wait(2)

        def disconnect(self):
            return None

    bridge = MQTTBridge()
    bridge._client = Client()
    controller, heartbeat, closed = shutdown_context(monkeypatch, watchdog, bridge)
    result = []

    def shutdown():
        result.append(
            main._shutdown_main_loop(controller, bridge, threading.Event(), heartbeat, timeout=1.5)
        )
        watcher_done.set()

    thread = threading.Thread(target=shutdown)
    thread.start()
    try:
        assert mqtt_entered.wait(1)
        assert not closed.is_set()
        if release_first == "mqtt":
            mqtt_release.set()
            assert not closed.wait(0.025)
            writer_release.set()
        else:
            writer_release.set()
            assert watchdog.stop(timeout=1)
            assert not closed.is_set()
            mqtt_release.set()
        assert watcher_done.wait(1)
        assert result == [True]
        assert closed.is_set()
        controller.victron.close.assert_called_once()
    finally:
        writer_release.set()
        mqtt_release.set()
        thread.join(timeout=2)
        watchdog.stop()
    assert not thread.is_alive()


def test_blocked_mqtt_callback_exhausts_one_budget_and_never_closes_shared_io(monkeypatch):
    release, entered, cleanup_done = threading.Event(), threading.Event(), threading.Event()
    bridge = MQTTBridge()

    class Client:
        def loop_stop(self):
            entered.set()
            assert release.wait(2)

        def disconnect(self):
            cleanup_done.set()

    bridge._client = Client()
    controller, heartbeat, closed = shutdown_context(monkeypatch, quiet_worker(), bridge)
    started = time.monotonic()
    try:
        assert (
            main._shutdown_main_loop(controller, bridge, threading.Event(), heartbeat, timeout=0.08)
            is False
        )
        assert entered.is_set() and time.monotonic() - started < 0.3
        assert not closed.is_set()
    finally:
        release.set()
        assert cleanup_done.wait(1)
    assert not closed.is_set()


def test_active_watchdog_timeout_also_refuses_shared_io_close(monkeypatch):
    watchdog = quiet_worker()
    watchdog.stop.return_value = False
    controller, heartbeat, closed = shutdown_context(monkeypatch, watchdog, None)
    assert (
        main._shutdown_main_loop(controller, None, threading.Event(), heartbeat, timeout=0.02)
        is False
    )
    assert not closed.is_set()


def test_main_join_cancellation_is_not_swallowed_or_authorized_as_quiescence(monkeypatch):
    watchdog = quiet_worker()
    watchdog.stop.side_effect = KeyboardInterrupt
    controller, heartbeat, closed = shutdown_context(monkeypatch, watchdog, None)
    with pytest.raises(KeyboardInterrupt):
        main._shutdown_main_loop(controller, None, threading.Event(), heartbeat, timeout=0.02)
    assert not closed.is_set()
    controller.ha.request_stop.assert_called_once()
    controller.victron.request_stop.assert_called_once()


def test_shutdown_request_rejects_new_mqtt_commands():
    bridge = MQTTBridge()
    callback = Mock()
    bridge.register_callback("toggle", callback)
    bridge._on_message(
        None,
        None,
        SimpleNamespace(
            topic=f"{bridge.prefix}/cmd/toggle",
            payload=b'{"key":"only_charging","state":true}',
            retain=False,
        ),
    )
    callback.assert_called_once_with({"key": "only_charging", "state": True})
    callback.reset_mock()
    bridge.request_stop()
    bridge._on_message(
        None,
        None,
        SimpleNamespace(
            topic=f"{bridge.prefix}/cmd/toggle",
            payload=b'{"key":"only_charging","state":true}',
            retain=False,
        ),
    )
    callback.assert_not_called()


@pytest.mark.parametrize("circuit_open", [False, True])
def test_ha_idle_wait_wakes_immediately_without_session_race(monkeypatch, circuit_open):
    client = HomeAssistantClient()
    entered = threading.Event()
    original_wait = client._stop_event.wait

    def idle_wait(timeout):
        entered.set()
        return original_wait(timeout)

    monkeypatch.setattr(client._stop_event, "wait", idle_wait)
    monkeypatch.setattr(client, "_poll_all", lambda: None)
    monkeypatch.setattr("inverter_control.homeassistant.HA_POLL_INTERVAL", 60)
    client._circuit_open = circuit_open
    client._circuit_open_time = time.time()
    client.start()
    try:
        assert entered.wait(1)
        assert client.stop(timeout=0.2) is True
    finally:
        client.stop(timeout=1)


def test_ha_inflight_request_keeps_session_until_thread_finishes(monkeypatch):
    client = HomeAssistantClient()
    entered, release = threading.Event(), threading.Event()

    def poll():
        entered.set()
        release.wait(2)

    monkeypatch.setattr(client, "_poll_all", poll)
    close = Mock()
    monkeypatch.setattr(client._session, "close", close)
    client.start()
    try:
        assert entered.wait(1)
        assert client.stop(timeout=0) is False
        close.assert_not_called()
        thread = client._thread
        client.start()
        assert client._thread is thread
    finally:
        release.set()
        assert client.stop(timeout=1) is True
    close.assert_called_once()


def test_main_requests_every_stop_before_first_join(monkeypatch):
    watchdog = quiet_worker()
    bridge = SimpleNamespace(request_stop=Mock(), disconnect=Mock(return_value=True))
    controller, heartbeat, _closed = shutdown_context(monkeypatch, watchdog, bridge)
    heartbeat_stop = threading.Event()

    def first_join(timeout):
        assert timeout <= 0.5
        assert heartbeat_stop.is_set()
        for request in (
            bridge.request_stop,
            controller.request_stop_auxiliary_readers,
            controller.ha.request_stop,
            controller.victron.request_stop,
            controller.grid_filter.request_stop,
            controller.derived_grid_filter.request_stop,
            main.request_stop_console_server,
        ):
            request.assert_called_once()
        return True

    watchdog.stop.side_effect = first_join
    assert main._shutdown_main_loop(controller, bridge, heartbeat_stop, heartbeat, timeout=0.5)


def test_console_sender_lock_consumes_same_budget(monkeypatch):
    from inverter_control import console_server

    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(console_server, "_clients_lock", lock)
    monkeypatch.setattr(console_server, "_server_socket", None)
    join = Mock()
    monkeypatch.setattr(
        console_server, "_server_thread", SimpleNamespace(join=join, is_alive=lambda: False)
    )
    try:
        assert console_server.stop_server(timeout=0.01) is False
        join.assert_not_called()
    finally:
        lock.release()
    assert console_server.stop_server(timeout=0.1) is True
    assert 0 <= join.call_args.kwargs["timeout"] <= 0.1


def test_native_close_is_not_requested_while_polling_is_alive():
    from inverter_control.victron import VictronDBus

    entered, release = threading.Event(), threading.Event()

    def poll():
        entered.set()
        release.wait(2)

    victron = VictronDBus.__new__(VictronDBus)
    victron._poll_stop_event = threading.Event()
    victron._poll_thread = threading.Thread(target=poll)
    victron._native_write, victron._native = Mock(), Mock()
    victron._poll_thread.start()
    try:
        assert entered.wait(1)
        assert victron.close(timeout=0) is False
        victron._native_write.close.assert_not_called()
        victron._native.close.assert_not_called()
    finally:
        release.set()
        victron._poll_thread.join(timeout=1)
    assert victron.close(timeout=0.1) is True
    victron._native_write.close.assert_called_once()
    victron._native.close.assert_called_once()


def test_native_close_request_failure_is_not_reported_as_success(monkeypatch):
    controller, heartbeat, _closed = shutdown_context(monkeypatch, quiet_worker(), None)
    controller.victron.close.side_effect = None
    controller.victron.close.return_value = False
    assert (
        main._shutdown_main_loop(controller, None, threading.Event(), heartbeat, timeout=0.1)
        is False
    )


def test_mqtt_actual_callback_write_finishes_before_shared_io_close(monkeypatch):
    watchdog_entered, watchdog_release = threading.Event(), threading.Event()
    mqtt_write_entered, mqtt_write_release = threading.Event(), threading.Event()
    callback_entered, disconnect_entered, callback_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    writes = []

    def physical_write(value):
        writes.append(value)
        if value == 100:
            watchdog_entered.set()
            assert watchdog_release.wait(2)
        else:
            assert value == 200
            mqtt_write_entered.set()
            assert mqtt_write_release.wait(2)
        return True

    victron = SimpleNamespace(set_grid_setpoint=physical_write)
    watchdog = HardwareWatchdog(victron, check_interval=10)
    watchdog._override_value = 100
    watchdog.start()
    assert watchdog_entered.wait(1)
    bridge = MQTTBridge()

    def command(value):
        callback_entered.set()
        watchdog.set_setpoint_override(value["value"])
        callback_done.set()

    bridge.register_callback("setpoint_override", command)
    callback_thread = threading.Thread(
        target=bridge._on_message,
        args=(
            None,
            None,
            SimpleNamespace(
                topic=f"{bridge.prefix}/cmd/setpoint_override",
                payload=b'{"value":200}',
                retain=False,
            ),
        ),
    )
    callback_thread.start()
    assert callback_entered.wait(1)

    class Client:
        def loop_stop(self):
            disconnect_entered.set()
            callback_thread.join(timeout=2)
            assert not callback_thread.is_alive()

        def disconnect(self):
            assert callback_done.is_set()

    bridge._client = Client()
    controller, heartbeat, closed = shutdown_context(monkeypatch, watchdog, bridge)
    victron.request_stop = controller.victron.request_stop
    victron.stop_polling = controller.victron.stop_polling
    victron.close = controller.victron.close
    controller.victron = victron
    results = []
    shutdown = threading.Thread(
        target=lambda: results.append(
            main._shutdown_main_loop(controller, bridge, threading.Event(), heartbeat, timeout=1.5)
        )
    )
    shutdown.start()
    try:
        assert disconnect_entered.wait(1)
        assert not closed.is_set()
        watchdog_release.set()
        assert mqtt_write_entered.wait(1)
        assert not closed.is_set() and not callback_done.is_set()
        mqtt_write_release.set()
        shutdown.join(timeout=1)
        assert results == [True] and closed.is_set()
        assert writes == [100, 200]
    finally:
        watchdog_release.set()
        mqtt_write_release.set()
        callback_thread.join(timeout=1)
        shutdown.join(timeout=1)
        watchdog.stop()


def test_console_sender_timeout_keeps_live_handle_and_reports_incomplete(monkeypatch):
    from inverter_control import console_server

    entered, release = threading.Event(), threading.Event()

    def sender():
        entered.set()
        release.wait(2)

    thread = threading.Thread(target=sender)
    monkeypatch.setattr(console_server, "_server_socket", None)
    monkeypatch.setattr(console_server, "_server_thread", None)
    monkeypatch.setattr(console_server, "_sender_thread", thread)
    monkeypatch.setattr(console_server, "_clients_lock", threading.Lock())
    monkeypatch.setattr(console_server, "_clients", set())
    thread.start()
    try:
        assert entered.wait(1)
        assert console_server.stop_server(timeout=0.01) is False
        assert console_server._sender_thread is thread
        assert thread.is_alive()
    finally:
        release.set()
        thread.join(timeout=1)
    assert console_server.stop_server(timeout=0.1) is True
    assert console_server._sender_thread is None


def test_console_client_accepted_before_stop_is_closed_after_late_admission(monkeypatch):
    from inverter_control import console_server as console

    entered, release = threading.Event(), threading.Event()

    class Client:
        closed = False

        def setblocking(self, value):
            entered.set()
            assert release.wait(1)

        def sendall(self, payload):
            pass

        def close(self):
            self.closed = True

    client = Client()

    class Listener:
        def accept(self):
            return client, ("fixture", 1)

        def close(self):
            pass

    class JoinedThread(threading.Thread):
        def join(self, timeout=None):
            # Let admission finish only after stop has cleared the client set.
            release.set()
            return super().join(timeout)

    monkeypatch.setattr(console, "_clients", set())
    monkeypatch.setattr(console, "_clients_lock", threading.Lock())
    monkeypatch.setattr(console, "_console_buffer", [])
    monkeypatch.setattr(console, "_server_socket", Listener())
    monkeypatch.setattr(console, "_running", True)
    monkeypatch.setattr(console, "_sender_thread", None)
    thread = JoinedThread(target=console._accept_clients, daemon=True)
    monkeypatch.setattr(console, "_server_thread", thread)
    thread.start()
    try:
        assert entered.wait(1)
        assert console.stop_server(timeout=0.5) is True
        assert not thread.is_alive()
        assert not console._clients
        assert client.closed
    finally:
        release.set()
        thread.join(1)
        client.close()


def test_blocked_mqtt_publisher_does_not_report_complete_or_close_shared_io(monkeypatch):
    release, entered = threading.Event(), threading.Event()

    def publish():
        entered.set()
        release.wait(3)

    bridge = MQTTBridge()
    bridge._publish_thread = threading.Thread(target=publish)
    bridge._client = SimpleNamespace(loop_stop=Mock(), disconnect=Mock())
    bridge._publish_thread.start()
    controller, heartbeat, close_requested = shutdown_context(monkeypatch, quiet_worker(), bridge)
    try:
        assert entered.wait(1)
        assert (
            main._shutdown_main_loop(controller, bridge, threading.Event(), heartbeat, timeout=1.2)
            is False
        )
        bridge._client.loop_stop.assert_called_once()
        bridge._client.disconnect.assert_called_once()
        assert not close_requested.is_set()
    finally:
        release.set()
        bridge._publish_thread.join(1)

"""Confirmed writes must not share telemetry callbacks or reconnect work."""

import threading
from unittest.mock import AsyncMock, Mock, patch

import pytest
from dbus_fast import Message, MessageType, Variant
from test_dbus_native_timeouts import EndpointBus, flush_loop, reply

from inverter_control.dbus_native import BUSITEM_INTERFACE
from inverter_control.victron import VictronDBus

SERVICE = "com.victronenergy.system"


@pytest.fixture
def facade():
    with (
        patch("inverter_control.victron.USE_NATIVE_DBUS", True),
        patch.object(VictronDBus, "_discover_services"),
        patch.object(VictronDBus, "_start_background_polling"),
    ):
        v = VictronDBus(test_mode=False)
    workers = []
    for index, client in enumerate((v._native, v._native_write)):
        loop = client._ensure_loop()
        ready = threading.Event()

        def started(loop=loop, ready=ready):
            workers.append((loop, threading.current_thread()))
            ready.set()

        loop.call_soon_threadsafe(started)
        assert ready.wait(1)
        client._bus = EndpointBus()
        client._bus.unique_name = f":1.{index + 1}"
    v._native._armed_subscriptions = set(v._native._subscriptions)
    yield v
    v.close()
    for loop, worker in workers:
        worker.join(2)
        assert not worker.is_alive()
        loop.close()


@pytest.mark.parametrize("shared_loop", [True, False])
def test_write_acceptance_with_blocked_telemetry_signal(facade, shared_loop):
    """The old shared-loop route times out; the isolated route confirms its ACK."""
    telemetry, writer = facade._native, facade._native_write
    entered, release = threading.Event(), threading.Event()

    def blocked_signal(*_args):
        entered.set()
        assert release.wait(3)

    telemetry.add_signal_handler(blocked_signal)
    telemetry._sender_service[":1.42"] = SERVICE
    signal = Message(
        message_type=MessageType.SIGNAL,
        path="/Ac/Grid/L1/Power",
        interface=BUSITEM_INTERFACE,
        member="PropertiesChanged",
        body=[{"Value": Variant("i", 17)}],
        signature="a{sv}",
        serial=1,
        sender=":1.42",
    )
    telemetry._loop.call_soon_threadsafe(telemetry._handle_message, signal)
    assert entered.wait(1)
    results = []
    caller = threading.Thread(
        target=lambda: results.append(facade._dbus_set(SERVICE, "/SlowWrite", 42))
    )
    with (
        patch.object(facade, "_native_write", telemetry if shared_loop else writer),
        patch.object(facade, "_safe_subprocess", return_value=None) as cli,
    ):
        try:
            caller.start()
            if shared_loop:
                caller.join(1)
                assert results == [False]
                cli.assert_called_once()
            else:
                assert writer._bus.started.wait(1)
                assert results == []  # Dispatch alone is not acceptance.
                writer._loop.call_soon_threadsafe(
                    writer._bus.pending["/SlowWrite"].set_result, reply([0], "u")
                )
                caller.join(1)
                assert results == [True]
                cli.assert_not_called()
            assert not release.is_set()
            assert not telemetry._bus.messages
            assert not writer._subscriptions
            assert writer.on_reconnect is None
        finally:
            release.set()
            caller.join(2)
    flush_loop(telemetry)
    assert not telemetry._bus.messages  # The expired shared-loop write is never sent later.


def test_writer_reconnect_does_not_reseed_or_disconnect_telemetry(facade):
    telemetry, writer = facade._native, facade._native_write
    failed = writer._bus
    with (
        patch.object(failed, "call", side_effect=ConnectionError("writer socket closed")),
        patch.object(facade, "_safe_subprocess", return_value=None),
    ):
        assert not facade._dbus_set(SERVICE, "/Setpoint", 42)
    assert failed.disconnections == 1
    assert telemetry.subscriptions_healthy()
    assert facade._dbus_get(SERVICE, "/Ac/Grid/L1/Power") == "17"

    replacement = EndpointBus()
    replacement.add_message_handler = Mock()

    class Connector:
        def __init__(self, **_kwargs):
            pass

        connect = AsyncMock(return_value=replacement)

    writer._fail_until = 0
    with (
        patch("dbus_fast.aio.message_bus.MessageBus", Connector),
        patch.object(telemetry, "_replay_subscriptions") as reseed,
        patch.object(facade, "_safe_subprocess") as cli,
    ):
        assert facade._dbus_set(SERVICE, "/Setpoint", 0)
    reseed.assert_not_called()
    cli.assert_not_called()
    assert [message.member for message in replacement.messages] == ["SetValue"]
    assert telemetry.subscriptions_healthy()
    assert telemetry._bus.disconnections == 0


def test_telemetry_disconnect_does_not_drop_writer(facade):
    telemetry, writer = facade._native, facade._native_write
    with (
        patch.object(telemetry._bus, "call", side_effect=ConnectionError("telemetry closed")),
        patch.object(facade, "_safe_subprocess", return_value=None),
    ):
        assert facade._dbus_get(SERVICE, "/Ac/Grid/L1/Power") is None
    with patch.object(facade, "_safe_subprocess") as cli:
        assert facade._dbus_set(SERVICE, "/Setpoint", 0)
    cli.assert_not_called()
    assert writer._bus.disconnections == 0
    assert writer._fail_until == 0


@pytest.mark.parametrize("fallback,accepted", [(None, False), ("   int32 0\n", True)])
def test_write_timeout_cancels_pending_ack_and_keeps_confirmed_cli_fallback(
    facade, fallback, accepted
):
    writer = facade._native_write
    native_set = writer.set_value

    def short_deadline(service, path, value, value_type):
        return native_set(service, path, value, value_type, timeout=0.03)

    with (
        patch.object(writer, "set_value", side_effect=short_deadline),
        patch.object(facade, "_safe_subprocess", return_value=fallback) as cli,
    ):
        assert facade._dbus_set(SERVICE, "/SlowWrite", 42) is accepted
    assert writer._bus.cancelled.wait(1)
    flush_loop(writer)
    assert writer._bus.pending["/SlowWrite"].cancelled()
    assert not writer._bus._method_return_handlers
    assert "--print-reply" in cli.call_args.args[0]
    assert facade._native.subscriptions_healthy()


def test_write_lock_still_serializes_callers(facade):
    writer = facade._native_write
    results = []
    first = threading.Thread(
        target=lambda: results.append(facade._dbus_set(SERVICE, "/SlowFirst", 1))
    )
    second = threading.Thread(
        target=lambda: results.append(facade._dbus_set(SERVICE, "/Second", 2))
    )
    first.start()
    assert writer._bus.started.wait(1)
    try:
        second.start()
        assert facade._set_lock.locked()
        assert len(writer._bus.messages) == 1
    finally:
        writer._loop.call_soon_threadsafe(
            writer._bus.pending["/SlowFirst"].set_result, reply([0], "u")
        )
        first.join(2)
        second.join(2)
    assert results == [True, True]
    assert [message.body[0].value for message in writer._bus.messages] == [1, 2]


@pytest.mark.parametrize("fallback,accepted", [("int32 0\n", True), ("int32 2\n", False)])
def test_native_failure_and_fallback_cannot_be_overtaken(facade, fallback, accepted):
    """A later write must stay after both transport attempts of the first write."""
    native_failed, second_done = threading.Event(), threading.Event()
    calls, results = [], {}

    class HandoffLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            self.lock.acquire()

        def __exit__(self, *_args):
            self.lock.release()
            # Exercise the valid scheduling interleaving when an implementation
            # releases its lock between native failure and CLI fallback.
            if threading.current_thread() is first and calls == [("native", 1)]:
                assert second_done.wait(2)

    def native_set(_service, _path, value, _type):
        calls.append(("native", value))
        if value == 1:
            native_failed.set()
            return False
        return True

    def cli_set(command, **_kwargs):
        calls.append(("cli", int(command[-1].rsplit(":", 1)[1])))
        return fallback

    def later_write():
        if native_failed.wait(2):
            results[2] = facade._dbus_set(SERVICE, "/Setpoint", 2)
        second_done.set()

    first = threading.Thread(
        target=lambda: results.update({1: facade._dbus_set(SERVICE, "/Setpoint", 1)})
    )
    second = threading.Thread(target=later_write)
    with (
        patch.object(facade, "_set_lock", HandoffLock()),
        patch.object(facade._native_write, "set_value", side_effect=native_set),
        patch.object(facade, "_safe_subprocess", side_effect=cli_set),
    ):
        second.start()
        first.start()
        first.join(3)
        second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert results == {1: accepted, 2: True}
    assert calls == [("native", 1), ("cli", 1), ("native", 2)]


@pytest.mark.parametrize("fallback,accepted", [("int32 0\n", True), ("int32 2\n", False)])
def test_blocked_failure_logger_cannot_delay_fallback_or_later_zero(facade, fallback, accepted):
    logging_entered, release_logger = threading.Event(), threading.Event()
    calls, results, errors_at_log = [], {}, []
    facade._consecutive_errors = 5

    def native_set(_service, _path, value, _type):
        calls.append(("native", value))
        return value == 0

    def cli_set(_command, **_kwargs):
        calls.append(("cli", 1))
        return fallback

    def blocked_warning(message):
        if message.startswith("Native D-Bus set failed:"):
            errors_at_log.append(facade._consecutive_errors)
            logging_entered.set()
            assert release_logger.wait(3)

    first = threading.Thread(
        target=lambda: results.update({1: facade._dbus_set(SERVICE, "/Setpoint", 1)})
    )
    second = threading.Thread(
        target=lambda: results.update({0: facade._dbus_set(SERVICE, "/Setpoint", 0)})
    )
    with (
        patch.object(facade._native_write, "set_value", side_effect=native_set),
        patch.object(facade, "_safe_subprocess", side_effect=cli_set),
        patch("inverter_control.victron.logger.warning", side_effect=blocked_warning),
    ):
        try:
            first.start()
            assert logging_entered.wait(1)
            assert calls == [("native", 1), ("cli", 1)]
            assert errors_at_log == [0 if accepted else 6]
            assert not facade._set_lock.locked()
            second.start()
            second.join(1)
            assert results == {0: True}
            assert not release_logger.is_set()
        finally:
            release_logger.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert calls == [("native", 1), ("cli", 1), ("native", 0)]
    assert results == {1: accepted, 0: True}
    assert facade._consecutive_errors == 0


@pytest.mark.parametrize("body,signature", [([1], "u"), ([], ""), ([False], "b"), (["bad"], "s")])
@pytest.mark.parametrize("fallback,accepted", [("int32 0\n", True), ("int32 2\n", False)])
def test_real_native_rejection_logs_only_after_cli_and_unlock(
    facade, body, signature, fallback, accepted
):
    """The real setter must not emit a second warning inside the facade's lock."""
    writer = facade._native_write
    logging_entered, release_logger = threading.Event(), threading.Event()
    calls, results, errors_at_log = [], {}, []
    facade._consecutive_errors = 5

    async def native_call(message):
        value = message.body[0].value
        calls.append(("native", value))
        return reply(body, signature) if value else reply([0], "u")

    def cli_set(command, **_kwargs):
        calls.append(("cli", int(command[-1].rsplit(":", 1)[1])))
        return fallback

    def blocked_warning(*_args):
        if not logging_entered.is_set():
            errors_at_log.append(facade._consecutive_errors)
            logging_entered.set()
            assert release_logger.wait(3)

    first = threading.Thread(
        target=lambda: results.update({1: facade._dbus_set(SERVICE, "/Setpoint", 1)})
    )
    zero = threading.Thread(
        target=lambda: results.update({0: facade._dbus_set(SERVICE, "/Setpoint", 0)})
    )
    with (
        patch.object(writer._bus, "call", side_effect=native_call),
        patch.object(facade, "_safe_subprocess", side_effect=cli_set),
        patch("inverter_control.dbus_native.logger.warning", side_effect=blocked_warning),
    ):
        try:
            first.start()
            assert logging_entered.wait(1)
            assert calls == [("native", 1), ("cli", 1)]
            assert errors_at_log == [0 if accepted else 6]
            assert not facade._set_lock.locked()
            zero.start()
            zero.join(1)
            assert results == {0: True}
            assert not release_logger.is_set()
        finally:
            release_logger.set()
            first.join(3)
            if zero.ident is not None:
                zero.join(3)
    assert not first.is_alive() and not zero.is_alive()
    assert calls == [("native", 1), ("cli", 1), ("native", 0)]
    assert results == {1: accepted, 0: True}
    assert facade._consecutive_errors == 0
    assert writer._fail_until == 0
    assert writer._bus.disconnections == 0


def test_diagnostics_follow_writer_and_close_releases_both_clients(facade):
    telemetry, writer = facade._native, facade._native_write
    read_bus, write_bus = telemetry._bus, writer._bus
    with patch("inverter_control.dbus_native.SLOW_SET_TIMING_MS", 0):
        assert facade._dbus_set(SERVICE, "/Setpoint", 0)
    assert facade.drain_write_timings()[0]["sender"] == ":1.2"
    assert not facade.drain_write_timings()
    assert not telemetry.drain_write_timings()
    facade._poll_thread = threading.Thread(target=facade._poll_stop_event.wait, daemon=True)
    facade._poll_thread.start()
    facade.close()
    assert not facade._poll_thread.is_alive()
    assert not writer.is_connected()
    assert not telemetry.is_connected()
    assert (read_bus.disconnections, write_bus.disconnections) == (1, 1)


def test_close_is_bounded_and_late_poll_cannot_reconnect_native_clients(facade):
    telemetry, writer = facade._native, facade._native_write
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    late_reads = []

    def blocked_poll():
        entered.set()
        if release.wait(5):
            late_reads.append(facade._dbus_get(SERVICE, "/Ac/Grid/L1/Power"))

    def close():
        facade.close()
        closed.set()

    poller = threading.Thread(target=facade._poll_loop, daemon=True)
    closer = threading.Thread(target=close, daemon=True)
    facade._poll_thread = poller
    with (
        patch.object(facade, "_check_rescan_needed"),
        patch.object(facade, "_poll_all", side_effect=blocked_poll) as poll,
        patch.object(facade, "_safe_subprocess", return_value=None) as cli,
        patch.object(telemetry, "_connect") as read_connect,
        patch.object(writer, "_connect") as write_connect,
    ):
        try:
            poller.start()
            assert entered.wait(1)
            closer.start()
            assert closed.wait(2)  # A blocked poll cannot hold shutdown indefinitely.
            assert poller.is_alive()
            assert not release.is_set()
            assert telemetry._fail_until == writer._fail_until == float("inf")
            release.set()
            poller.join(2)
            assert not poller.is_alive()
            assert late_reads == [None]
            poll.assert_called_once()
            cli.assert_called_once()  # An in-flight poll retains its existing fallback.
            assert writer._get_bus() is None
            read_connect.assert_not_called()
            write_connect.assert_not_called()
            assert telemetry._loop is writer._loop is None
        finally:
            release.set()
            poller.join(2)
            if closer.ident is not None:
                closer.join(2)

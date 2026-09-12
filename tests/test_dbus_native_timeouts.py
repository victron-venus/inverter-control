"""Request deadlines must not tear down a healthy shared native connection."""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from dbus_fast import Message, MessageType, Variant

from inverter_control import victron
from inverter_control.dbus_native import NativeDbusClient

SERVICE = "com.victronenergy.system"
GRID_PATH = "/Ac/Grid/L1/Power"


def reply(body, signature="v"):
    return Message(
        message_type=MessageType.METHOD_RETURN,
        serial=1,
        reply_serial=1,
        body=body,
        signature=signature,
    )


class EndpointBus:
    """Model dbus-fast's per-serial handlers and cancellable reply futures."""

    def __init__(self):
        self.connected = True
        self.messages = []
        self.pending = {}
        self._method_return_handlers = {}
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self.disconnections = 0

    async def call(self, message):
        message.serial = len(self.messages) + 1
        self.messages.append(message)
        if message.path.startswith("/Slow"):
            future = asyncio.get_running_loop().create_future()
            self.pending[message.path] = future
            self._method_return_handlers[message.serial] = future
            self.started.set()
            try:
                return await future
            finally:
                self.cancelled.set()
        if message.member == "SetValue":
            return reply([0], "u")
        return reply([Variant("i", 17)])

    def disconnect(self):
        self.connected = False
        self.disconnections += 1


@pytest.fixture(name="native")
def native_client():
    client = NativeDbusClient()
    loop = asyncio.new_event_loop()

    def run():
        client._loop_thread_id = threading.get_ident()
        loop.run_forever()

    worker = threading.Thread(target=run, daemon=True)
    client._loop = loop
    worker.start()
    client._bus = EndpointBus()
    client._subscriptions = {"grid-rule"}
    client._armed_subscriptions = {"grid-rule"}
    client._sender_service = {":1.42": SERVICE}
    yield client
    client.close()
    worker.join(2)
    assert not worker.is_alive()
    loop.close()


def flush_loop(native):
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), native._loop).result(1)


def test_slow_read_preserves_grid_connection_subscriptions_and_cli_free_reads(native, caplog):
    bus = native._bus
    with caplog.at_level("DEBUG", logger="inverter-control"):
        assert native.get_value(SERVICE, "/Slow", timeout=0.03) is None
    assert bus.cancelled.wait(1)
    flush_loop(native)
    assert not bus._method_return_handlers
    assert native._bus is bus
    assert native._fail_until == 0
    assert bus.disconnections == 0
    assert native.subscriptions_healthy()
    assert native._sender_service == {":1.42": SERVICE}
    assert "TimeoutError" in caplog.text
    assert "GetValue//Slow" in caplog.text

    # Exercise the real facade, so a healthy grid read cannot silently fork CLI.
    v = victron.VictronDBus(test_mode=True)
    v._native = native
    with patch.object(v, "_safe_subprocess") as cli:
        assert v._dbus_get(SERVICE, GRID_PATH) == "17"
    cli.assert_not_called()


@pytest.mark.parametrize(
    "error", [TimeoutError(), ValueError("bad endpoint reply"), RuntimeError("endpoint error")]
)
def test_endpoint_exception_does_not_trigger_global_cooldown(native, error):
    bus = native._bus

    async def fail(_message):
        raise error

    with patch.object(bus, "call", side_effect=fail):
        assert native.get_value(SERVICE, "/Slow") is None
    assert native._bus is bus
    assert native.subscriptions_healthy()
    assert native.get_value(SERVICE, GRID_PATH) == "17"


def test_timed_out_write_stays_unaccepted_and_late_ack_is_discarded(native):
    bus = native._bus
    assert not native.set_value(SERVICE, "/SlowWrite", 100, timeout=0.03)
    assert bus.cancelled.wait(1)
    flush_loop(native)
    assert bus.pending["/SlowWrite"].cancelled()
    assert not bus._method_return_handlers
    # dbus-fast ignores late replies whose serial has no handler. There is no
    # outstanding operation that can turn the earlier False into acceptance.
    assert native.set_value(SERVICE, "/Setpoint", 0)
    assert [message.body[0].value for message in bus.messages] == [100, 0]
    assert native._bus is bus


def test_cancelled_request_does_not_remove_another_pending_serial(native):
    bus = native._bus
    result = []
    healthy = threading.Thread(
        target=lambda: result.append(native.get_value(SERVICE, "/SlowHealthy", timeout=1))
    )
    healthy.start()
    assert bus.started.wait(1)
    assert native.get_value(SERVICE, "/SlowTimeout", timeout=0.03) is None
    flush_loop(native)
    assert set(bus._method_return_handlers) == {1}
    native._loop.call_soon_threadsafe(
        bus.pending["/SlowHealthy"].set_result, reply([Variant("i", 23)])
    )
    healthy.join(1)
    assert not healthy.is_alive()
    assert result == ["23"]
    assert not bus._method_return_handlers
    assert native.subscriptions_healthy()


def test_overdue_write_queued_on_stalled_loop_is_never_dispatched(native):
    bus = native._bus
    blocked = threading.Event()
    release = threading.Event()

    def stall():
        blocked.set()
        release.wait(1)

    native._loop.call_soon_threadsafe(stall)
    assert blocked.wait(1)
    try:
        assert not native.set_value(SERVICE, "/Setpoint", 100, timeout=0.03)
    finally:
        release.set()
    flush_loop(native)
    assert not bus.messages
    assert native.set_value(SERVICE, "/Setpoint", 0)
    assert [message.body[0].value for message in bus.messages] == [0]
    assert native._bus is bus


def test_same_loop_write_is_not_scheduled_or_accepted(native):
    bus = native._bus

    async def probe():
        return native.set_value(SERVICE, "/Setpoint", 100)

    with native._state_lock:
        assert not asyncio.run_coroutine_threadsafe(probe(), native._loop).result(1)
    flush_loop(native)
    assert not bus.messages
    assert not native._tasks
    assert native.subscriptions_healthy()


def test_same_loop_helper_never_constructs_or_schedules_coroutine(native):
    factory = MagicMock(side_effect=AssertionError("must not construct request"))

    async def probe():
        return native._call_on_loop(factory, 0.5)

    assert asyncio.run_coroutine_threadsafe(probe(), native._loop).result(1) is None
    factory.assert_not_called()


@pytest.mark.parametrize("error", [ConnectionError("closed"), EOFError(), OSError("socket")])
def test_transport_failure_reconnects_and_rearms_subscriptions(native, error):
    failed = native._bus

    async def fail(_message):
        raise error

    with patch.object(failed, "call", side_effect=fail):
        assert native.get_value(SERVICE, GRID_PATH) is None
    assert native._bus is None
    assert native._fail_until > 0
    assert failed.disconnections == 1
    assert not native.subscriptions_healthy()

    replacement = EndpointBus()

    def reconnect():
        native._bus = replacement
        native._replay_subscriptions()

    native._fail_until = 0
    with patch.object(native, "_connect", side_effect=reconnect):
        assert native.get_value(SERVICE, GRID_PATH) == "17"
    assert native.subscriptions_healthy()
    assert any(message.member == "AddMatch" for message in replacement.messages)


def test_timeout_on_disconnected_bus_still_marks_transport_failed(native):
    bus = native._bus

    async def disconnect_during_call(_message):
        bus.connected = False
        raise TimeoutError()

    with patch.object(bus, "call", side_effect=disconnect_during_call):
        assert native.get_value(SERVICE, GRID_PATH) is None
    assert native._bus is None
    assert native._fail_until > 0


def test_late_old_bus_failure_cannot_drop_replacement(native):
    failed = native._bus
    replacement = EndpointBus()

    async def replace_then_fail(_message):
        native._bus = replacement
        failed.connected = False
        raise ConnectionError("old connection closed")

    with patch.object(failed, "call", side_effect=replace_then_fail):
        assert native.get_value(SERVICE, GRID_PATH) is None
    assert native._bus is replacement
    assert native._fail_until == 0
    assert replacement.disconnections == 0
    assert native.subscriptions_healthy()
    assert native.get_value(SERVICE, GRID_PATH) == "17"


def test_interrupted_wait_cancels_submitted_operation(native):
    future = MagicMock()
    future.result.side_effect = RuntimeError("control cycle interrupted")

    def submit(coroutine, _loop):
        coroutine.close()
        return future

    with patch("asyncio.run_coroutine_threadsafe", side_effect=submit):
        with pytest.raises(RuntimeError, match="control cycle interrupted"):
            native._call_on_loop(lambda: asyncio.sleep(0), 0.5)
    future.cancel.assert_called_once()


def test_closed_loop_enters_cooldown_and_next_connection_rearms(native):
    working_loop = native._loop
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    native._loop = closed_loop
    try:
        assert native.get_value(SERVICE, GRID_PATH) is None
        assert native._bus is None
        assert native._fail_until > 0
        assert not native.subscriptions_healthy()
    finally:
        native._loop = working_loop

    replacement = EndpointBus()

    def reconnect():
        native._bus = replacement
        native._replay_subscriptions()

    native._fail_until = 0
    with patch.object(native, "_connect", side_effect=reconnect):
        assert native.get_value(SERVICE, GRID_PATH) == "17"
    assert native.subscriptions_healthy()
    assert any(message.member == "AddMatch" for message in replacement.messages)

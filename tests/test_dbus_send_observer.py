"""Actual dbus-fast call/send dispatch, with a controlled writer and no socket."""

import asyncio
from unittest.mock import Mock

import pytest
from dbus_fast import Message, MessageType
from dbus_fast.aio.message_bus import MessageBus

from inverter_control.dbus_native import NativeDbusClient, _SendTimingBusMixin


class ObservedBus(_SendTimingBusMixin, MessageBus):
    pass


def make_bus(*, immediate=False, error=None):
    # Skip __init__ because dbus-fast opens a socket there. Keep the real
    # MessageBus.call/send and BaseMessageBus._call/reply-handler behavior.
    bus = ObservedBus.__new__(ObservedBus)
    bus._loop = asyncio.get_running_loop()
    bus._send_observations = {}
    bus._send_observers_closed = False
    bus._send_observer_loop = bus._loop
    bus._method_return_handlers = {}
    bus._name_owners = {}
    bus._user_message_handlers = []
    bus._serial = 0
    bus.unique_name = ":1.42"

    class Writer:
        def __init__(self):
            self.calls = []
            self.sent = asyncio.Event()

        def schedule_write(self, message, future):
            self.calls.append((message, future))
            self.sent.set()
            if error is not None:
                raise error
            if immediate:
                future.set_result(None)

        def reply(self, body=0, error=None):
            message = self.calls[-1][0]
            response = Message(
                message_type=MessageType.METHOD_RETURN,
                reply_serial=message.serial,
                signature="i",
                body=[body],
            )
            if error is None:
                for handler in tuple(bus._user_message_handlers):
                    assert handler(response) is None
            bus._method_return_handlers.pop(message.serial)(response, error)
            return response

    bus._writer = Writer()
    return bus


def message():
    return Message(destination="com.example.Test", path="/Setpoint", member="SetValue")


@pytest.mark.parametrize("immediate", [False, True])
@pytest.mark.parametrize("body", [0, 1])
def test_actual_call_send_observation_does_not_supply_or_replace_ack(immediate, body):
    async def run():
        bus = make_bus(immediate=immediate)
        client = NativeDbusClient(observe_write_send=True)
        timing = {}
        request = message()
        task = asyncio.create_task(client._call_message(bus, request, timing=timing))
        await bus._writer.sent.wait()
        sent, future = bus._writer.calls[0]
        assert sent is request
        assert not task.done(), "send completion cannot replace the application ACK"
        assert timing["call_started_at"] <= timing["send_started_at"] <= timing["send_returned_at"]
        if not immediate:
            assert "send_done_observed_at" not in timing
            future.set_result(None)
            await asyncio.sleep(0)  # Dispatch already queued callbacks; no wall-clock wait.
        assert timing["send_returned_at"] <= timing["send_done_observed_at"]
        assert timing["send_future_cancelled"] is False
        assert not task.done()
        response = bus._writer.reply(body)
        assert await task is response
        assert response.body == [body]
        assert (
            timing["send_done_observed_at"]
            <= timing["reply_observed_at"]
            <= timing["call_finished_at"]
        )
        assert len(bus._writer.calls) == 1
        assert not bus._send_observations
        assert not bus._user_message_handlers
        assert not bus._method_return_handlers

    asyncio.run(run())


def test_override_returns_identical_future_and_ignores_unobserved_message():
    async def run():
        bus = make_bus(immediate=True)
        request, unrelated = message(), message()
        timing = {}
        stop = bus.observe_send(request, timing)
        first = bus.send(unrelated)
        assert first is bus._writer.calls[0][1]
        assert not timing
        second = bus.send(request)
        assert second is bus._writer.calls[1][1]
        assert "send_done_observed_at" in timing
        stop()
        assert not bus._send_observations

    asyncio.run(run())


@pytest.mark.parametrize("finish", ["cancel", "disconnect", "timeout"])
def test_pending_send_observer_is_removed_without_cancelling_transport_future(finish):
    async def run():
        bus = make_bus()
        client = NativeDbusClient(observe_write_send=True)
        timing = {}
        task = asyncio.create_task(client._call_message(bus, message(), timing=timing))
        await bus._writer.sent.wait()
        future = bus._writer.calls[0][1]
        assert future._callbacks  # Only the optional send observer is attached.
        if finish == "cancel":
            task.cancel()
            error = asyncio.CancelledError
        else:
            error = ConnectionError if finish == "disconnect" else TimeoutError
            bus._writer.reply(error=error("transport ended"))
        with pytest.raises(error):
            await asyncio.wait_for(task, timeout=1.0)
        assert not future.done(), "diagnostic cleanup must not cancel the queued send"
        assert not future._callbacks
        assert not bus._send_observations
        assert not bus._user_message_handlers
        assert not bus._method_return_handlers
        frozen = dict(timing)
        future.set_result(None)
        await asyncio.sleep(0)
        assert timing == frozen
        assert "send_done_observed_at" not in timing
        assert len(bus._writer.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_future_error_or_cancellation_is_neither_consumed_nor_an_ack(cancelled):
    async def run():
        bus = make_bus()
        client = NativeDbusClient(observe_write_send=True)
        timing = {}
        task = asyncio.create_task(client._call_message(bus, message(), timing=timing))
        await bus._writer.sent.wait()
        future = bus._writer.calls[0][1]
        if cancelled:
            future.cancel()
        else:
            failure = OSError("send failed")
            future.set_exception(failure)
        await asyncio.sleep(0)
        assert not task.done()
        assert timing["send_future_cancelled"] is cancelled
        if not cancelled:
            # The observer did not call exception()/result() or hide the failure.
            assert future._log_traceback
            assert future.exception() is failure  # Test cleanup consumes it now.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        assert not bus._send_observations

    asyncio.run(run())


def test_original_send_exception_propagates_once_and_cleans_observers():
    async def run():
        failure = RuntimeError("marshalling rejected")
        bus = make_bus(error=failure)
        client = NativeDbusClient(observe_write_send=True)
        timing = {}
        with pytest.raises(RuntimeError) as caught:
            await client._call_message(bus, message(), timing=timing)
        assert caught.value is failure
        assert len(bus._writer.calls) == 1
        assert "send_started_at" in timing
        assert "send_returned_at" not in timing
        assert "send_done_observed_at" not in timing
        assert not bus._send_observations
        assert not bus._method_return_handlers

    asyncio.run(run())


def test_observer_install_failure_does_not_change_transport(monkeypatch):
    async def run():
        bus = make_bus(immediate=True)
        monkeypatch.setattr(bus, "observe_send", Mock(side_effect=RuntimeError("unavailable")))
        timing = {}
        client = NativeDbusClient(observe_write_send=True)
        task = asyncio.create_task(client._call_message(bus, message(), timing=timing))
        await bus._writer.sent.wait()
        response = bus._writer.reply()
        assert await task is response
        assert "send_started_at" not in timing
        assert len(bus._writer.calls) == 1

    asyncio.run(run())


def test_reader_uses_original_bus_class_and_writer_alone_opts_in(monkeypatch):
    class ConnectedBus:
        connected = True

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def connect(self):
            return self

        def add_message_handler(self, handler):
            self.handler = handler

    monkeypatch.setattr("dbus_fast.aio.message_bus.MessageBus", ConnectedBus)
    for opt_in in (False, True):
        client = NativeDbusClient(observe_write_send=opt_in)
        monkeypatch.setattr(client, "_ensure_loop", lambda: None)
        monkeypatch.setattr(
            client, "_call_on_loop", lambda factory, _timeout: asyncio.run(factory())
        )
        client._connect()
        assert isinstance(client._bus, _SendTimingBusMixin) is opt_in
        if not opt_in:
            assert type(client._bus) is ConnectedBus
            assert not hasattr(client._bus, "_send_observations")


@pytest.mark.parametrize("mark_failed", [False, True])
def test_actual_close_removes_pending_observer_even_after_bus_dropped(mark_failed):
    loop = asyncio.new_event_loop()
    client = NativeDbusClient(observe_write_send=True)
    client._loop = loop

    async def start():
        bus = make_bus()
        bus.disconnect = lambda: None  # No socket or transport action in this fixture.
        client._bus = bus
        timing = {}
        task = asyncio.create_task(client._call_message(bus, message(), timing=timing))
        await bus._writer.sent.wait()
        return bus, timing, task, bus._writer.calls[0][1]

    bus, timing, task, future = loop.run_until_complete(start())
    try:
        assert len(bus._send_observations) == 1
        assert future._callbacks

        def shutdown():
            if mark_failed:
                client._mark_failure(bus)
                assert client._bus is None
            client.close()

        loop.call_soon(shutdown)
        loop.run_forever()
        assert not task.done(), "test exercises close before call.finally can clean up"
        assert not future.done(), "observer cleanup must not cancel transport"
        assert not future._callbacks
        assert not bus._send_observations
        assert bus.observe_send(message(), {}) is None
        assert len(bus._writer.calls) == 1
        frozen = dict(timing)
        future.set_result(None)
        loop.run_until_complete(asyncio.sleep(0))
        assert timing == frozen
    finally:
        # Existing transport task teardown is outside the diagnostic change.
        # Resume/cancel only here so the offline fixture releases its own task.
        task.cancel()
        loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()


def test_stopped_loop_cleanup_suppresses_already_queued_done_callback():
    loop = asyncio.new_event_loop()

    async def start():
        bus = make_bus()
        timing = {}
        request = message()
        bus.observe_send(request, timing)
        future = bus.send(request)
        return bus, timing, future

    bus, timing, future = loop.run_until_complete(start())
    try:
        future.set_result(None)  # Callback is queued but the loop is stopped.
        bus.stop_observing_sends()
        frozen = dict(timing)
        loop.run_until_complete(asyncio.sleep(0))
        assert timing == frozen
        assert "send_done_observed_at" not in timing
        assert not bus._send_observations
        assert bus.observe_send(message(), {}) is None
    finally:
        loop.close()


def test_stale_failure_cleans_only_old_bus_observers():
    async def run():
        old_bus, new_bus = make_bus(), make_bus()
        futures = []
        for bus in (old_bus, new_bus):
            request = message()
            bus.observe_send(request, {})
            futures.append(bus.send(request))
        client = NativeDbusClient(observe_write_send=True)
        client._loop = asyncio.get_running_loop()
        client._bus = new_bus
        client._mark_failure(old_bus)
        await asyncio.sleep(0)
        assert client._bus is new_bus
        assert client._fail_until == 0
        assert not old_bus._send_observations
        assert not futures[0]._callbacks
        assert len(new_bus._send_observations) == 1
        assert futures[1]._callbacks
        assert all(not future.done() for future in futures)
        new_bus.stop_observing_sends()
        await asyncio.sleep(0)

    asyncio.run(run())


def test_connection_replacement_cleans_observers_on_original_stopped_loop(monkeypatch):
    loop = asyncio.new_event_loop()

    async def start():
        bus = make_bus()
        request = message()
        bus.observe_send(request, {})
        return bus, bus.send(request)

    bus, future = loop.run_until_complete(start())
    try:
        client = NativeDbusClient(observe_write_send=True)
        client._bus = bus
        client._loop = loop
        replacement = Mock(connected=True)
        monkeypatch.setattr(client, "_ensure_loop", lambda: loop)
        monkeypatch.setattr(client, "_call_on_loop", lambda *_args: replacement)
        client._connect()
        assert client._bus is replacement
        assert not bus._send_observations
        assert not future._callbacks
        assert not future.done()
        assert bus.observe_send(message(), {}) is None
    finally:
        loop.close()

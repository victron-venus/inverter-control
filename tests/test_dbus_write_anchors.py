"""Diagnostics must observe ACK delivery without consuming or delaying writes."""

import asyncio
import os
import threading
import time
from unittest.mock import Mock, patch

import pytest
from dbus_fast import Message, MessageType

from inverter_control.dbus_native import NativeDbusClient
from inverter_control.victron import VictronDBus

SERVICE = "com.victronenergy.system"


class ObservedBus:
    """Model public message handlers before dbus-fast's normal reply future."""

    def __init__(self, *, body=0, message_type=MessageType.METHOD_RETURN, block=False):
        self.handlers = []
        self._method_return_handlers = {}
        self.observed = asyncio.Event()
        self.resume = asyncio.Event()
        self.body = body
        self.message_type = message_type
        self.block = block
        self.connected = True
        self.unique_name = ":1.42"
        self.calls = 0
        self.handler_results = []

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        self.handlers.remove(handler)

    async def call(self, message):
        self.calls += 1
        message.serial = self.calls
        self._method_return_handlers[message.serial] = object()
        # Unrelated message types and serials must not be mistaken for this ACK.
        unrelated = Message(
            message_type=MessageType.SIGNAL,
            path="/Other",
            interface="example.Other",
            member="Changed",
        )
        wrong_serial = Message(message_type=MessageType.METHOD_RETURN, reply_serial=999)
        for handler in tuple(self.handlers):
            assert handler(unrelated) is None
            assert handler(wrong_serial) is None
        self.before_reply = time.monotonic()
        reply = Message(
            message_type=self.message_type,
            reply_serial=message.serial,
            error_name="example.Error" if self.message_type == MessageType.ERROR else None,
            signature="i",
            body=[self.body],
        )
        for handler in tuple(self.handlers):
            self.handler_results.append(handler(reply))
        self.observed.set()
        if self.block:
            await self.resume.wait()
        return reply


@pytest.mark.parametrize("kind", [MessageType.METHOD_RETURN, MessageType.ERROR])
def test_public_observer_is_nonconsuming_and_splits_reply_from_resume(kind):
    async def run():
        client = NativeDbusClient()
        bus = ObservedBus(message_type=kind, block=True)
        message = Message(destination=SERVICE, path="/Setpoint", member="SetValue")
        timing = {}
        pending = asyncio.create_task(client._call_message(bus, message, timing=timing))
        await asyncio.wait_for(bus.observed.wait(), 1)
        assert not pending.done()
        assert timing["call_started_at"] <= bus.before_reply <= timing["reply_observed_at"]
        assert "call_finished_at" not in timing
        assert bus.handler_results == [None]
        bus.resume.set()
        response = await pending
        assert response.message_type == kind
        assert response.body == [0]
        assert timing["reply_observed_at"] <= timing["call_finished_at"]
        assert bus.calls == 1
        assert not bus.handlers
        assert not bus._method_return_handlers

    asyncio.run(run())


def test_cancelled_observer_is_removed_without_changing_other_handlers():
    async def run():
        client, bus = NativeDbusClient(), ObservedBus(block=True)
        other = lambda _reply: None
        bus.handlers.append(other)
        bus._method_return_handlers[99] = object()
        timing = {}
        message = Message(destination=SERVICE, path="/Setpoint", member="SetValue")
        pending = asyncio.create_task(client._call_message(bus, message, timing=timing))
        await asyncio.wait_for(bus.observed.wait(), 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert bus.handlers == [other]
        assert set(bus._method_return_handlers) == {99}
        assert timing["call_finished_at"] >= timing["reply_observed_at"]

    asyncio.run(run())


def test_unavailable_public_observer_still_calls_transport_once():
    async def run():
        client, bus = NativeDbusClient(), ObservedBus()
        bus.add_message_handler = Mock(side_effect=RuntimeError("diagnostics unavailable"))
        timing = {}
        message = Message(destination=SERVICE, path="/Setpoint", member="SetValue")
        result = await client._call_message(bus, message, timing=timing)
        assert result.body == [0]
        assert "reply_observed_at" not in timing
        assert timing["call_finished_at"] >= timing["call_started_at"]
        assert not bus.handlers
        assert bus.calls == 1

    asyncio.run(run())


@pytest.mark.parametrize("native_enabled", [True, False])
def test_outer_lock_is_measured_before_native_or_cli_without_values(native_enabled):
    facade = VictronDBus(test_mode=True)
    writer = Mock()
    writer.set_value.return_value = True
    writer.drain_write_timings.return_value = []
    facade._native_write = writer if native_enabled else None
    requested, release = threading.Event(), threading.Event()
    transport = threading.Event()

    class HeldLock:
        def __enter__(self):
            requested.set()
            assert release.wait(2)

        def __exit__(self, *_args):
            return False

    facade._set_lock = HeldLock()
    outcomes, tids = [], []

    def invoke():
        tids.append(threading.get_native_id())
        outcomes.append(facade._dbus_set(SERVICE, "/DoNotLogPath", 123456789))

    def send(*_args, **_kwargs):
        transport.set()
        return True

    writer.set_value.side_effect = send
    with patch.object(facade, "_safe_subprocess", return_value="int32 0") as cli:
        with patch("inverter_control.victron.SLOW_SET_TIMING_MS", 0):
            caller = threading.Thread(target=invoke)
            caller.start()
            assert requested.wait(1)
            assert not transport.is_set()
            cli.assert_not_called()
            release.set()
            caller.join(2)
            assert not caller.is_alive()
    assert outcomes == [True]
    (sample,) = facade.drain_write_timings()
    assert sample["phase"] == "write_lock"
    assert sample["pid"] == os.getpid()
    assert sample["caller_native_tid"] == tids[0]
    assert sample["anchors"]["lock_acquired_at"] >= sample["anchors"]["lock_requested_at"]
    assert sample["lock_wait_ms"] >= 0
    assert "123456789" not in repr(sample)
    assert "DoNotLogPath" not in repr(sample)
    assert not facade.drain_write_timings()
    if native_enabled:
        writer.set_value.assert_called_once_with(SERVICE, "/DoNotLogPath", 123456789, "int16")
        cli.assert_not_called()
    else:
        cli.assert_called_once()


def test_outer_lock_diagnostics_are_bounded_and_skip_fast_waits():
    facade = VictronDBus(test_mode=True)
    facade._record_write_lock_timing(1.0, 1.1)
    assert not facade.drain_write_timings()
    for index in range(70):
        facade._record_write_lock_timing(index, index + 0.25)
    samples = facade.drain_write_timings()
    assert len(samples) == 64
    assert [sample["anchors"]["lock_requested_at"] for sample in samples] == list(range(6, 70))
    assert all(sample["lock_wait_ms"] == 250 for sample in samples)


@pytest.mark.parametrize("diagnostic_queue", ["outer", "native"])
def test_busy_diagnostic_sink_cannot_block_a_real_native_write(diagnostic_queue):
    facade = VictronDBus(test_mode=True)
    native = NativeDbusClient()
    loop = native._ensure_loop()
    bus = ObservedBus()
    native._bus = bus
    facade._native_write = native
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    worker = []

    held_lock = (
        facade._write_lock_timings_lock
        if diagnostic_queue == "outer"
        else native._write_timings_lock
    )

    def hold_diagnostics():
        with held_lock:
            entered.set()
            assert release.wait(2)

    def write():
        worker.append(facade._dbus_set(SERVICE, "/Setpoint", 0))
        completed.set()

    holder = threading.Thread(target=hold_diagnostics)
    caller = threading.Thread(target=write)
    holder.start()
    assert entered.wait(1)
    try:
        with (
            patch("inverter_control.victron.SLOW_SET_TIMING_MS", 0),
            patch("inverter_control.dbus_native.SLOW_SET_TIMING_MS", 0),
        ):
            caller.start()
            assert completed.wait(1), "optional diagnostics blocked the native ACK"
            assert worker == [True]
            assert bus.calls == 1
            assert not release.is_set()
    finally:
        release.set()
        holder.join(2)
        caller.join(2)
        native.close()
        # close schedules loop.stop; wait without opening another connection.
        deadline = time.monotonic() + 1
        while loop.is_running() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not loop.is_running()
        loop.close()
    samples = facade.drain_write_timings()
    lost_phase = "write_lock" if diagnostic_queue == "outer" else "native_call"
    assert all(sample["phase"] != lost_phase for sample in samples)

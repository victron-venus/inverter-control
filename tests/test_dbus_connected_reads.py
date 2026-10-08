"""Reconciliation reads never enter connection setup, even across lifecycle races."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dbus_fast import MessageType, Variant

from inverter_control.dbus_native import NativeDbusClient

SERVICE = "com.victronenergy.system"


class ReadBus:
    def __init__(self):
        self.connected = True
        self.messages = []
        self.cancelled = threading.Event()
        self._method_return_handlers = {}

    async def call(self, message):
        self.messages.append(message)
        message.serial = len(self.messages)
        if message.path == "/Slow":
            future = asyncio.get_running_loop().create_future()
            self._method_return_handlers[message.serial] = future
            try:
                await asyncio.wait_for(future, timeout=1.0)
            finally:
                self.cancelled.set()
        value = (
            Variant("a{sv}", {"Ac/Grid/L1/Power": Variant("i", 0)})
            if message.path == "/"
            else Variant("i", 17)
        )
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[value])

    def disconnect(self):
        self.connected = False


@pytest.fixture
def client():
    native = NativeDbusClient()
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        native._loop_thread_id = threading.get_ident()
        loop.call_soon(ready.set)
        loop.run_forever()

    native._loop = loop
    native._bus = ReadBus()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert ready.wait(1)
    yield native
    native.close()
    if loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    worker.join(1)
    assert not worker.is_alive()
    loop.close()


def test_connected_reads_decode_and_use_only_existing_bus(client):
    with (
        patch.object(client, "_get_bus", side_effect=AssertionError("reconnect path")),
        patch.object(client, "_ensure_loop", side_effect=AssertionError("loop creation")),
    ):
        assert client.get_value_connected(SERVICE, "/Power") == "17"
        assert client.get_values_connected(SERVICE) == {"/Ac/Grid/L1/Power": "0"}
    assert all(message.member == "GetValue" for message in client._bus.messages)


def test_cold_client_never_connects_or_starts_a_loop():
    native = NativeDbusClient()
    with (
        patch.object(native, "_connect", side_effect=AssertionError("connect")),
        patch.object(native, "_ensure_loop", side_effect=AssertionError("new loop")),
    ):
        assert native.get_values_connected(SERVICE) is None
        assert native.get_value_connected(SERVICE, "/Power") is None
    assert native._loop is None


def test_concurrent_reconnect_lock_is_not_waited_on(client):
    result = []
    done = threading.Event()

    def read():
        result.append(client.get_values_connected(SERVICE))
        done.set()

    client._state_lock.acquire()
    worker = threading.Thread(target=read, daemon=True)
    try:
        worker.start()
        assert done.wait(1), "bounded read waited behind the reconnect lock"
        assert result == [None]
        assert not client._bus.messages
    finally:
        client._state_lock.release()
        worker.join(1)


@pytest.mark.parametrize("race", ["disconnect", "replace", "close"])
def test_lifecycle_change_between_snapshot_and_dispatch_never_reconnects(client, race):
    bus, loop = client._bus, client._loop
    call = client._call_on_loop

    def race_before_submit(*args, **kwargs):
        assert kwargs["existing_loop"] is loop
        if race == "disconnect":
            bus.connected = False
        elif race == "replace":
            client._bus = ReadBus()
        else:
            client._bus = client._loop = None
        return call(*args, **kwargs)

    try:
        with (
            patch.object(client, "_call_on_loop", side_effect=race_before_submit),
            patch.object(client, "_get_bus", side_effect=AssertionError("reconnect")),
            patch.object(client, "_ensure_loop", side_effect=AssertionError("new loop")),
        ):
            assert client.get_values_connected(SERVICE) is None
        assert not bus.messages
        if client._bus is not None:
            assert not client._bus.messages
    finally:
        client._bus, client._loop = bus, loop


def test_connection_error_does_not_wait_on_failure_management_lock(client):
    call = client._call_on_loop

    async def fail(_message):
        raise ConnectionError("bus disconnected")

    def hold_during_call(*args, **kwargs):
        with client._state_lock:
            return call(*args, **kwargs)

    with (
        patch.object(client._bus, "call", side_effect=fail),
        patch.object(client, "_call_on_loop", side_effect=hold_during_call),
        patch.object(client, "_mark_failure", side_effect=AssertionError("blocking repair")),
    ):
        assert client.get_values_connected(SERVICE, timeout=0.03) is None


def test_silent_endpoint_is_cancelled_without_losing_connection(client):
    bus = client._bus
    assert client.get_value_connected(SERVICE, "/Slow", timeout=0.03) is None
    assert bus.cancelled.wait(1)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), client._loop).result(1)
    assert not bus._method_return_handlers
    assert client._bus is bus
    assert bus.connected
    assert client.get_value_connected(SERVICE, "/Power") == "17"


def test_expired_read_queued_on_busy_loop_is_never_dispatched(client):
    blocked, release = threading.Event(), threading.Event()

    def stall():
        blocked.set()
        release.wait(1)

    client._loop.call_soon_threadsafe(stall)
    assert blocked.wait(1)
    try:
        assert client.get_values_connected(SERVICE, timeout=0.03) is None
        assert not client._bus.messages
    finally:
        release.set()
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), client._loop).result(1)
    assert not client._bus.messages

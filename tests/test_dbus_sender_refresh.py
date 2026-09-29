"""Sender discovery stays bounded and cannot revive owners from a stale reply."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from dbus_fast import MessageType

from inverter_control.dbus_native import NativeDbusClient

SERVICE = "com.victronenergy.system"
OTHER_SERVICE = "com.victronenergy.grid.lab"


class OwnerBus:
    """An asynchronous daemon reply that can be held across reconnect/owner change."""

    connected = True

    def __init__(self, owner=":1.42"):
        self.owner = owner
        self.messages = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._method_return_handlers = {}

    async def call(self, message):
        message.serial = len(self.messages) + 1
        self.messages.append(message)
        self._method_return_handlers[message.serial] = object()
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[self.owner])


def make_client(bus):
    client = NativeDbusClient()
    client._bus = bus
    client._subscription_services.add(SERVICE)
    return client


def test_sender_refresh_burst_has_one_inflight_lookup():
    async def scenario():
        bus = OwnerBus()
        client = make_client(bus)
        tasks = [asyncio.create_task(client._refresh_sender_map()) for _ in range(40)]
        try:
            await bus.started.wait()
            await asyncio.sleep(0)
            assert len(bus.messages) == 1
        finally:
            bus.release.set()
            await asyncio.gather(*tasks)
        assert client._sender_service == {":1.42": SERVICE}
        assert not bus._method_return_handlers

    asyncio.run(scenario())


def test_unknown_sender_burst_does_not_create_one_task_per_sender():
    async def scenario():
        bus = OwnerBus()
        client = make_client(bus)
        try:
            for number in range(1000):
                client._handle_unresolved_sender(f":1.{number}")
            assert len(client._tasks) == 1
        finally:
            bus.release.set()
            await asyncio.gather(*client._tasks)

    asyncio.run(scenario())


def test_silent_daemon_lookup_expires_and_releases_pending_handler():
    async def scenario():
        bus = OwnerBus()
        client = make_client(bus)
        with patch("inverter_control.dbus_native.MATCH_TIMEOUT", 0.01):
            await asyncio.wait_for(client._refresh_sender_map(), timeout=0.2)
        assert not client._sender_service
        assert not bus._method_return_handlers
        # A later refresh remains possible on the same healthy connection.
        bus.release.set()
        await client._refresh_sender_map()
        assert client._sender_service == {":1.42": SERVICE}

    asyncio.run(scenario())


def test_replaced_connection_cannot_publish_late_sender_mapping():
    async def scenario():
        old_bus = OwnerBus(":1.1")
        client = make_client(old_bus)
        old_task = asyncio.create_task(client._refresh_sender_map())
        await old_bus.started.wait()
        replacement = OwnerBus(":1.99")
        replacement.release.set()
        client._bus = replacement
        try:
            await client._refresh_sender_map()
            assert client._sender_service == {":1.99": SERVICE}
        finally:
            old_bus.release.set()
            await old_task
        assert client._sender_service == {":1.99": SERVICE}

    asyncio.run(scenario())


def test_owner_changed_signal_wins_over_older_daemon_reply():
    async def scenario():
        bus = OwnerBus(":1.1")
        client = make_client(bus)
        task = asyncio.create_task(client._refresh_sender_map())
        await bus.started.wait()
        client._handle_name_owner_changed(SimpleNamespace(body=[SERVICE, ":1.1", ":1.2"]))
        bus.release.set()
        await task
        assert client._sender_service == {":1.2": SERVICE}

    asyncio.run(scenario())


def test_subscription_added_during_refresh_is_resolved_in_same_round():
    async def scenario():
        bus = OwnerBus()
        client = make_client(bus)
        task = asyncio.create_task(client._refresh_sender_map())
        await bus.started.wait()
        client._subscription_services.add(OTHER_SERVICE)
        second = asyncio.create_task(client._refresh_sender_map())
        await asyncio.sleep(0)
        bus.release.set()
        await asyncio.gather(task, second)
        assert [message.body[0] for message in bus.messages] == [SERVICE, OTHER_SERVICE]

    asyncio.run(scenario())

#!/usr/bin/env python3
"""
Persistent native D-Bus client (dbus_fast) for Victron BusItem Get/Set.

Replaces per-call `dbus-send` subprocess invocations in the hot path with a
single long-lived system-bus connection served by a dedicated event-loop
thread. Callers stay synchronous: requests are scheduled onto the loop and
awaited with the same timeouts the CLI version used.

Verified against dbus-fast 2.21.1 on Venus OS:
- MessageBus must be constructed with the explicit system bus address.
- Messages whose body contains Variants need an explicit signature ("v"),
  otherwise the argument is dropped ("expected variant" InvalidArgs).
- BusItem.SetValue replies METHOD_RETURN with body [0] on success.
"""

import asyncio
import logging
import os
import threading
import time

logger = logging.getLogger("inverter-control")

try:
    from dbus_fast import MessageType, Variant

    _DBUS_FAST_AVAILABLE = True
except ImportError:  # Development machines without dbus-fast: CLI fallback only
    _DBUS_FAST_AVAILABLE = False

BUSITEM_INTERFACE = "com.victronenergy.BusItem"
SYSTEM_BUS_ADDRESS = os.environ.get(
    "DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/var/run/dbus/system_bus_socket"
)
CONNECT_TIMEOUT = 2.0
# After a failure, skip native calls briefly so the CLI fallback takes over
# while the bus recovers; next call after cooldown reconnects automatically.
RECONNECT_COOLDOWN = 5.0

# D-Bus signature type codes for the variant types we write.
TYPE_CODES = {
    "int16": "n",
    "uint16": "q",
    "int32": "i",
    "uint32": "u",
    "int64": "x",
    "double": "d",
    "string": "s",
}


class NativeDbusClient:
    """
    Persistent system-bus connection over dbus_fast with its own event loop.

    Thread-safe: any thread may call get_value/set_value concurrently; the
    single bus connection pipelines requests, so telemetry reads and setpoint
    writes never block each other on a lock.
    """

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus = None
        self._state_lock = threading.Lock()
        self._fail_until = 0.0

    # ------------------------------------------------------------------ #
    # Loop / connection lifecycle                                        #
    # ------------------------------------------------------------------ #

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start the dedicated event-loop thread on first use."""
        if self._loop is not None and self._loop.is_running():
            return self._loop
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True, name="dbus-native").start()
        self._loop = loop
        return loop

    def _connect(self):
        from dbus_fast.aio.message_bus import MessageBus

        async def _connect():
            return await MessageBus(bus_address=SYSTEM_BUS_ADDRESS).connect()

        self._bus = asyncio.run_coroutine_threadsafe(_connect(), self._ensure_loop()).result(
            CONNECT_TIMEOUT
        )

    def _get_bus(self):
        """Return a connected bus or None (cooldown active / connect failed)."""
        with self._state_lock:
            if time.time() < self._fail_until:
                return None
            try:
                if self._bus is None:
                    self._connect()
                return self._bus
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Native D-Bus connect failed: %s", e)
                self._fail_until = time.time() + RECONNECT_COOLDOWN
                self._bus = None
                return None

    def _mark_failure(self):
        """Enter reconnect cooldown and drop the broken connection."""
        with self._state_lock:
            self._fail_until = time.time() + RECONNECT_COOLDOWN
            bus, self._bus = self._bus, None
        if bus is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(bus.disconnect(), self._loop).result(1.0)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Native D-Bus disconnect failed: %s", e)

    def close(self):
        """Stop the event-loop thread and release the connection."""
        with self._state_lock:
            bus, self._bus = self._bus, None
            self._fail_until = float("inf")
            loop, self._loop = self._loop, None
        if bus is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(bus.disconnect(), loop).result(1.0)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Native D-Bus disconnect failed: %s", e)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)

    # ------------------------------------------------------------------ #
    # BusItem calls                                                      #
    # ------------------------------------------------------------------ #

    def call_busitem(
        self,
        service: str,
        path: str,
        member: str,
        body: list | None = None,
        timeout: float = 0.3,
    ):
        """Call a com.victronenergy.BusItem method; reply Message or None."""
        if not _DBUS_FAST_AVAILABLE:
            return None
        bus = self._get_bus()
        if bus is None:
            return None
        from dbus_fast import Message

        try:
            kwargs = {"body": body, "signature": "v"} if body is not None else {}
            message = Message(
                destination=service,
                path=path,
                interface=BUSITEM_INTERFACE,
                member=member,
                **kwargs,
            )
            future = asyncio.run_coroutine_threadsafe(bus.call(message), self._loop)
            reply = future.result(timeout)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Native D-Bus %s %s/%s failed: %s", service, member, path, e)
            self._mark_failure()
            return None
        if reply.message_type != MessageType.METHOD_RETURN:
            logger.debug(
                "D-Bus %s %s returned %s %s",
                service,
                path,
                getattr(reply, "error_name", None),
                reply.body,
            )
            return None
        return reply

    def get_value(self, service: str, path: str, timeout: float = 0.3) -> str | None:
        """GetValue as string (same shape the dbus-send literal parse produced)."""
        reply = self.call_busitem(service, path, "GetValue", timeout=timeout)
        if reply is None or not reply.body:
            return None
        value = getattr(reply.body[0], "value", None)
        return _format_value(value)

    def set_value(
        self,
        service: str,
        path: str,
        value,
        value_type: str = "int16",
        timeout: float = 0.3,
    ) -> bool:
        """SetValue with an explicitly typed variant. True on success (reply 0)."""
        code = TYPE_CODES.get(value_type)
        if code is None:
            logger.debug("Unsupported D-Bus set type: %s", value_type)
            return False
        reply = self.call_busitem(
            service, path, "SetValue", body=[Variant(code, value)], timeout=timeout
        )
        if reply is None:
            return False
        if reply.body and reply.body[0] != 0:
            logger.warning("SetValue %s%s rejected: %s", service, path, reply.body[0])
            return False
        return True


def _format_value(value) -> str | None:
    """Format a python value the way callers of _dbus_get expect.

    Numbers/strings keep their literal form; booleans become "1"/"0" like
    dbus-send literal output. Unsupported container types fall back to CLI.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return None

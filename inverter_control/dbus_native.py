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
DBUS_DAEMON = "org.freedesktop.DBus"
DBUS_DAEMON_PATH = "/org/freedesktop/DBus"
SYSTEM_BUS_ADDRESS = os.environ.get(
    "DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/var/run/dbus/system_bus_socket"
)
CONNECT_TIMEOUT = 2.0
MATCH_TIMEOUT = 1.0
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
        # Thread-safe lock for bus connection state. We use a simple Lock
        # because none of our methods that hold _state_lock call another
        # method that also tries to acquire it (no re-entrancy needed).
        self._state_lock = threading.Lock()
        self._fail_until = 0.0
        # Signal subscription support (PropertiesChanged)
        self._signal_handlers: list = []  # callbacks (service, path, value_str)
        self._handlers_lock = threading.Lock()
        # Armed match rules (strings), replayed after reconnect
        self._subscriptions: set[str] = set()
        # Well-known services behind the armed rules (for sender resolution)
        self._subscription_services: set[str] = set()
        # Sender unique bus name -> well-known service name. Path-keyed fast
        # inputs collide across services (vebus's bulk ItemsChanged carries its
        # own /Dc/0/* items), so handlers must know WHO sent a signal.
        self._sender_service: dict[str, str] = {}
        self._resolving_senders: set[str] = set()
        # Called after a lost connection is re-established, so the owner can
        # refetch initial values (signals only fire on change).
        self.on_reconnect = None

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
            bus = await MessageBus(bus_address=SYSTEM_BUS_ADDRESS).connect()
            bus.add_message_handler(self._handle_message)
            return bus

        self._bus = asyncio.run_coroutine_threadsafe(_connect(), self._ensure_loop()).result(
            CONNECT_TIMEOUT
        )
        if self._subscriptions:
            # Re-arm match rules; signals don't survive a disconnect
            self._replay_subscriptions()

    def _replay_subscriptions(self):
        """Re-arm match rules after a (re)connect; signals don't survive disconnects."""
        # Bus reattachment gives services new unique names; the old map lies.
        self._sender_service.clear()
        for rule in list(self._subscriptions):
            try:
                self._send_add_match(rule)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Native D-Bus re-subscribe failed (%s): %s", rule, e)
        if self._subscription_services and self._loop is not None and self._bus is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._refresh_sender_map(), self._loop)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Sender map refresh failed: %s", e)
        if self.on_reconnect is not None:
            try:
                self.on_reconnect()
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Native D-Bus on_reconnect handler failed: %s", e)

    def _build_rule(self, service: str, member: str, path: str) -> str:
        return (
            f"type='signal',sender='{service}',"
            f"interface='{BUSITEM_INTERFACE}',member='{member}',path='{path}'"
        )

    def _send_add_match(self, rule: str):
        from dbus_fast import Message

        message = Message(
            destination=DBUS_DAEMON,
            path=DBUS_DAEMON_PATH,
            interface="org.freedesktop.DBus",
            member="AddMatch",
            body=[rule],
            signature="s",
        )
        future = asyncio.run_coroutine_threadsafe(self._bus.call(message), self._loop)
        reply = future.result(MATCH_TIMEOUT)
        if reply.message_type != MessageType.METHOD_RETURN:
            raise ConnectionError(f"AddMatch rejected: {reply.message_type}")

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

    # ------------------------------------------------------------------ #
    # Signal subscriptions (BusItem change signals)                      #
    # ------------------------------------------------------------------ #
    # Venus services announce changes in two shapes, and which one they use
    # depends on the service implementation:
    #   - per-item PropertiesChanged on the item's object path
    #   - bulk ItemsChanged on "/" carrying {object_path: {Value, Text}}
    # Verified live: com.victronenergy.system (dbus-systemcalc-py) only emits
    # ItemsChanged; battery/vebus-style services emit per-item signals too.

    def add_signal_handler(self, callback):
        """Register callback(path: str, value: str | None) for matched signals."""

        with self._handlers_lock:
            self._signal_handlers.append(callback)

    def subscribe_signal(self, service: str, member: str, path: str) -> bool:
        """Arm one match rule. Idempotent; re-armed automatically after a
        reconnect. Initial values must still be fetched (signals fire on
        change only)."""
        rule = self._build_rule(service, member, path)
        if rule in self._subscriptions:
            return True

        bus = self._get_bus()
        if bus is None:
            return False
        try:
            self._send_add_match(rule)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Native D-Bus subscribe %s failed: %s", rule, e)
            return False
        self._subscriptions.add(rule)
        self._subscription_services.add(service)
        # Resolve the sender eagerly so the first signals already carry the
        # service tag; lazy refresh below covers services that come up later.
        if self._loop is not None and self._bus is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._refresh_sender_map(), self._loop)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Sender resolve scheduling failed: %s", e)
        return True

    async def _refresh_sender_map(self):
        """Map subscribed well-known names to their current unique senders."""
        from dbus_fast import Message

        for svc in list(self._subscription_services):
            try:
                reply = await self._bus.call(
                    Message(
                        destination=DBUS_DAEMON,
                        path=DBUS_DAEMON_PATH,
                        interface="org.freedesktop.DBus",
                        member="GetNameOwner",
                        body=[svc],
                        signature="s",
                    )
                )
                if reply.message_type == MessageType.METHOD_RETURN and reply.body:
                    self._sender_service[str(reply.body[0])] = svc
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("GetNameOwner %s failed: %s", svc, e)

    def subscribe_busitem(self, service: str, path: str) -> bool:
        """Forward per-item PropertiesChanged for one BusItem object."""
        return self.subscribe_signal(service, "PropertiesChanged", path)

    def subscribe_service_items(self, service: str) -> bool:
        """Forward the bulk ItemsChanged signal a service emits on '/'."""
        return self.subscribe_signal(service, "ItemsChanged", "/")

    def _handle_message(self, message):
        """Dispatch BusItem change signals to registered handlers.

        Runs on the event-loop thread; handlers are called inline and must be
        quick (they update caches only). Handlers receive the sender's
        well-known service name so path collisions between services (vebus vs
        battery both publish /Dc/0/*) can be routed correctly.
        """
        try:
            if message.message_type != MessageType.SIGNAL or message.interface != BUSITEM_INTERFACE:
                return
            sender = getattr(message, "sender", None)
            service = self._sender_service.get(sender) if sender else None
            if sender and service is None:
                # Owner not yet resolved (service appeared after subscribe);
                # refresh the map and drop this batch — reconcile covers it.
                if sender not in self._resolving_senders:
                    self._resolving_senders.add(sender)
                    try:
                        asyncio.ensure_future(self._refresh_and_clear(sender))
                    except RuntimeError:
                        self._resolving_senders.discard(sender)
                return
            if message.member == "ItemsChanged" and message.path == "/":
                items = message.body[0] if message.body else {}
                for obj_path, props in items.items():
                    self._dispatch(obj_path, props, service)
            elif message.member == "PropertiesChanged":
                props = message.body[0] if message.body else {}
                self._dispatch(message.path, props, service)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Native D-Bus signal dispatch failed: %s", e)

    async def _refresh_and_clear(self, sender: str):
        """Refresh the sender map, then stop skipping this sender."""
        await self._refresh_sender_map()
        self._resolving_senders.discard(sender)

    def _dispatch(self, path: str, props, service: str | None):
        value = getattr(props.get("Value"), "value", None)
        formatted = _format_value(value)
        with self._handlers_lock:
            handlers = list(self._signal_handlers)
        for callback in handlers:
            callback(service, path, formatted)


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

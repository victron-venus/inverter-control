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
import hashlib
import logging
import marshal
import os
import stat
import sys
import threading
import time
import types
from collections import deque

logger = logging.getLogger("inverter-control")

try:
    from dbus_fast import MessageType, Variant

    _DBUS_FAST_AVAILABLE = True
except ImportError:  # Development machines without dbus-fast: CLI fallback only
    _DBUS_FAST_AVAILABLE = False

BUSITEM_INTERFACE = "com.victronenergy.BusItem"
DBUS_DAEMON = "org.freedesktop.DBus"
DBUS_DAEMON_PATH = "/org/freedesktop/DBus"
NAME_OWNER_RULE = (
    "type='signal',sender='org.freedesktop.DBus',"
    "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
    "path='/org/freedesktop/DBus'"
)
SYSTEM_BUS_ADDRESS = os.environ.get(
    "DBUS_SYSTEM_BUS_ADDRESS", "unix:path=/var/run/dbus/system_bus_socket"
)
CONNECT_TIMEOUT = 2.0
MATCH_TIMEOUT = 1.0
# After a connection failure, skip native calls briefly so the CLI fallback takes over
# while the bus recovers; next call after cooldown reconnects automatically.
RECONNECT_COOLDOWN = 5.0
SLOW_SET_TIMING_MS = 200.0
PROVENANCE_SCHEMA = "dbus-send-provenance-v1"
PROVENANCE_MRO_LIMIT = 8
PROVENANCE_MODULE_LIMIT = 8
PROVENANCE_FILE_LIMIT = 4 * 1024 * 1024
PROVENANCE_TOTAL_FILE_LIMIT = 8 * 1024 * 1024
PROVENANCE_RECORD_LIMIT = 32768

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


def _provenance_name(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def _provenance_type(cls):
    if not isinstance(cls, type):
        return None
    return {
        "module": _provenance_name(cls.__module__),
        "qualname": _provenance_name(cls.__qualname__),
    }


def _provenance_code(code):
    """Background only: fingerprint the captured code object, never its globals."""
    if not isinstance(code, types.CodeType):
        return {"status": "no_python_code"}
    budget = [262144, 4096]

    def bounded(value, depth=0):
        budget[1] -= 1
        if depth > 12 or budget[1] < 0:
            return False
        if isinstance(value, types.CodeType):
            return all(
                bounded(item, depth + 1)
                for item in (
                    value.co_code,
                    value.co_consts,
                    value.co_names,
                    value.co_varnames,
                    value.co_freevars,
                    value.co_cellvars,
                    value.co_filename,
                    value.co_name,
                    value.co_qualname,
                    value.co_linetable,
                    value.co_exceptiontable,
                )
            )
        if isinstance(value, (tuple, frozenset)):
            return len(value) <= 1024 and all(bounded(item, depth + 1) for item in value)
        if isinstance(value, (bytes, str)):
            budget[0] -= len(value) * (4 if isinstance(value, str) else 1)
        elif isinstance(value, int):
            budget[0] -= max(1, value.bit_length() // 8 + 1)
        elif (
            value is not None and value is not Ellipsis and not isinstance(value, (float, complex))
        ):
            return False
        return budget[0] >= 0

    if not bounded(code):
        return {"status": "code_budget_exceeded"}
    encoded = marshal.dumps(code, 4)
    if len(encoded) > 1024 * 1024:
        return {"status": "code_budget_exceeded"}
    return {
        "status": "captured_python_code",
        "format": "python-marshal-v4-sha256",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _provenance_module(name, budget):
    """Background only; a current file hash is not proof of loaded binary bytes."""
    result = {"module": name, "file": None, "spec_origin": None}
    allowed = name in {"builtins", "_asyncio", "inverter_control.dbus_native"} or name.startswith(
        ("dbus_fast.", "asyncio.")
    )
    if not allowed:
        return {**result, "status": "module_not_allowlisted"}
    module = sys.modules.get(name)
    if module is None:
        return {**result, "status": "module_not_loaded"}
    path = getattr(module, "__file__", None)
    result["file"] = _provenance_name(path, 512)
    result["spec_origin"] = _provenance_name(
        getattr(getattr(module, "__spec__", None), "origin", None), 512
    )
    if not isinstance(path, str) or len(path) > 512:
        return {**result, "status": "no_bounded_file_path"}
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return {**result, "status": "not_regular_file"}
        if before.st_size > min(PROVENANCE_FILE_LIMIT, budget[0]):
            return {**result, "status": "file_budget_exceeded"}
        hasher = hashlib.sha256()
        size = 0
        while size < before.st_size:
            block = os.read(fd, min(65536, before.st_size - size))
            if not block:
                break
            size += len(block)
            budget[0] -= len(block)
            hasher.update(block)
        after = os.fstat(fd)
        if size != before.st_size or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            return {**result, "status": "file_changed_during_read"}
        return {
            **result,
            "status": "current_file_hashed",
            "bytes": size,
            "current_file_sha256": hasher.hexdigest(),
            "loaded_bytes_verified": False,
        }
    except OSError as error:
        return {**result, "status": "file_unavailable", "error_type": type(error).__name__}
    finally:
        if fd is not None:
            os.close(fd)


def _serialize_send_provenance(refs):
    """Called only by the existing background timing drain, outside its lock."""
    record = {
        "phase": "writer_provenance",
        "schema": PROVENANCE_SCHEMA,
        "clock": "monotonic",
        "observed_at": refs["observed_at"],
        "pid": refs["pid"],
        "loop_native_tid": refs["loop_native_tid"],
        "sender": _provenance_name(refs["sender"], 128),
        "send_returned": refs["send_returned"],
        "underlying_send": {
            "module": _provenance_name(refs["send_module"]),
            "qualname": _provenance_name(refs["send_qualname"]),
            "bound_callable_type": _provenance_type(refs["send_type"]),
            "code": _provenance_code(refs["send_code"]),
        },
    }
    modules = []
    if record["underlying_send"]["module"]:
        modules.append(record["underlying_send"]["module"])
    for role in ("bus", "writer", "future"):
        mro = [_provenance_type(cls) for cls in refs[role + "_mro"]]
        record[role] = {
            "class": mro[0] if mro else None,
            "mro": mro,
            "mro_truncated": refs[role + "_truncated"],
        }
        for descriptor in mro:
            if descriptor and descriptor["module"] and descriptor["module"] not in modules:
                modules.append(descriptor["module"])
    budget = [PROVENANCE_TOTAL_FILE_LIMIT]
    record["modules"] = [
        _provenance_module(name, budget) for name in modules[:PROVENANCE_MODULE_LIMIT]
    ]
    record["modules_truncated"] = len(modules) > PROVENANCE_MODULE_LIMIT
    # The existing sink logs dict repr, not JSON. Enforce its actual payload
    # bound after serialization, including UTF-8 expansion and escaping.
    if len(repr(record).encode("utf-8")) > PROVENANCE_RECORD_LIMIT:
        raise ValueError("provenance record budget exceeded")
    return record


class _SendObservation:
    """Observe a send Future without awaiting, cancelling or consuming its result."""

    def __init__(self, timing):
        self.timing = timing
        self.active = True
        self.future = None

    def started(self):
        try:
            self.timing["send_started_at"] = time.monotonic()
        except Exception:
            # Missing optional timing must never prevent the original send.
            pass

    def returned(self, future):
        try:
            self.timing["send_returned_at"] = time.monotonic()
            if not isinstance(future, asyncio.Future):
                return
            self.future = future
            if future.done():
                self._done(future)
            else:
                future.add_done_callback(self._done)
        except Exception:
            # Return the original Future even if diagnostic setup fails.
            pass

    def _done(self, future):
        if not self.active:
            return
        try:
            self.timing["send_done_observed_at"] = time.monotonic()
            self.timing["send_future_cancelled"] = future.cancelled()
            # Do not call result()/exception(): observation must neither
            # consume an error nor mistake Future completion for an ACK.
        except Exception:
            # A diagnostic callback must not raise into the transport loop.
            pass

    def close(self):
        self.active = False
        if self.future is not None:
            self.future.remove_done_callback(self._done)
            self.future = None


class _SendTimingBusMixin:
    """Opt-in on the writer only; public send/call retain their normal semantics."""

    def __init__(self, *args, **kwargs):
        self._send_observations = {}
        self._send_observers_closed = False
        self._send_observer_loop = asyncio.get_running_loop()
        self._send_provenance_sink = None
        self._send_provenance_taken = False
        super().__init__(*args, **kwargs)

    def observe_send(self, message, timing):
        if self._send_observers_closed:
            return None
        observation = _SendObservation(timing)
        self._send_observations[id(message)] = (message, observation)

        def stop():
            self._send_observations.pop(id(message), None)
            observation.close()

        return stop

    def stop_observing_sends(self):
        """Release only diagnostics, on their original loop before it stops."""

        def clear():
            self._send_observers_closed = True
            self._send_provenance_sink = None
            observations = tuple(self._send_observations.values())
            self._send_observations.clear()
            for _, observation in observations:
                observation.close()

        loop = self._send_observer_loop
        if loop.is_running():
            # Native close queues this before loop.stop, including when a
            # failed connection has already been dropped from client._bus.
            loop.call_soon_threadsafe(clear)
        else:
            clear()

    def _take_send_provenance(self, underlying_send):
        """Writer loop: retain bounded type/code refs, never instances or file I/O."""
        try:
            if getattr(self, "_send_provenance_taken", False) or not getattr(
                self, "_send_provenance_sink", None
            ):
                return None
            self._send_provenance_taken = True
            refs = {
                "observed_at": time.monotonic(),
                "pid": os.getpid(),
                "loop_native_tid": threading.get_native_id(),
                "sender": self.unique_name,
                "send_module": getattr(underlying_send, "__module__", None),
                "send_qualname": getattr(underlying_send, "__qualname__", None),
                "send_type": type(underlying_send),
                # Capture before invoking this exact bound callable. A later
                # reassignment of function.__code__ must not revise evidence.
                "send_code": getattr(underlying_send, "__code__", None),
            }
            for role, cls in (("bus", type(self)), ("writer", type(self._writer))):
                mro = cls.__mro__
                refs[role + "_mro"] = mro[:PROVENANCE_MRO_LIMIT]
                refs[role + "_truncated"] = len(mro) > PROVENANCE_MRO_LIMIT
            return refs
        except Exception:
            # Missing optional provenance must never prevent the original send.
            return None

    def _finish_send_provenance(self, refs, future, returned):
        if refs is None:
            return
        try:
            mro = type(future).__mro__ if returned else ()
            refs["future_mro"] = mro[:PROVENANCE_MRO_LIMIT]
            refs["future_truncated"] = len(mro) > PROVENANCE_MRO_LIMIT
            refs["send_returned"] = returned
            sink = self._send_provenance_sink
            if sink is not None:
                sink(refs)
        except Exception:
            # Diagnostics cannot replace the original transport result/error.
            pass

    def send(self, message):
        entry = self._send_observations.get(id(message))
        observation = entry[1] if entry is not None and entry[0] is message else None
        underlying_send = super().send
        refs = self._take_send_provenance(underlying_send) if observation is not None else None
        if observation is not None:
            observation.started()
        # Call the original transport once and return the very same Future.
        # In dbus-fast 2.21.1, this may write synchronously before returning.
        future = None
        returned = False
        try:
            future = underlying_send(message)
            returned = True
            if observation is not None:
                observation.returned(future)
            return future
        finally:
            self._finish_send_provenance(refs, future, returned)


class NativeDbusClient:
    """
    Persistent system-bus connection over dbus_fast with its own event loop.

    Thread-safe: any thread may call get_value/set_value concurrently; the
    single bus connection pipelines requests, so telemetry reads and setpoint
    writes never block each other on a lock.
    """

    def __init__(self, *, observe_write_send=False, write_diagnostics=None):
        self._observe_write_send = observe_write_send
        self._write_diagnostics = write_diagnostics
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._bus = None
        # Thread-safe lock for bus connection state. We use a simple Lock
        # because none of our methods that hold _state_lock call another
        # method that also tries to acquire it (no re-entrancy needed).
        self._state_lock = threading.Lock()
        self._fail_until = 0.0
        # Signal subscription support (PropertiesChanged)
        self._signal_handlers: list = []  # callbacks (service, path, value_str)
        # NameOwnerChanged signal handlers
        self._name_owner_handlers: list = []  # callbacks (service_name, old_owner, new_owner)
        self._handlers_lock = threading.Lock()
        # Armed match rules (strings), replayed after reconnect
        self._subscriptions: set[str] = set()
        self._optional_subscriptions: set[str] = set()
        self._armed_subscriptions: set[str] = set()
        # Well-known services behind the armed rules (for sender resolution)
        self._subscription_services: set[str] = set()
        # Sender unique bus name -> well-known service name. Path-keyed fast
        # inputs collide across services (vebus's bulk ItemsChanged carries its
        # own /Dc/0/* items), so handlers must know WHO sent a signal.
        self._sender_service: dict[str, str] = {}
        # Sender discovery is single-flight per connection. Unknown-sender
        # bursts must not allocate one task or daemon call per signal.
        self._sender_refresh_bus = None
        self._sender_refresh_after = 0.0
        self._sender_owner_versions: dict[str, int] = {}
        # Fire-and-forget task registry (see _track_task): keeps references to
        # background tasks so GC can't collect them mid-run.
        self._tasks: set[asyncio.Future] = set()
        # Called after a lost connection is re-established, so the owner can
        # refetch initial values (signals only fire on change).
        self.on_reconnect = None
        # Bounded diagnostics only. The performance worker drains these; no
        # logging or exporter I/O runs while a write caller waits for its ACK.
        self._write_timings = deque(maxlen=64)
        self._send_provenance = deque(maxlen=4)
        self._send_provenance_closed = False
        self._write_timings_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Loop / connection lifecycle                                        #
    # ------------------------------------------------------------------ #

    def _debug(self, event, message, *args, **context) -> None:
        if self._write_diagnostics is None:
            logger.debug(message, *args)
            return
        try:
            self._write_diagnostics.record(event, **context)
        except Exception:
            # A failing diagnostic buffer cannot alter a write or its fallback.
            pass

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start the dedicated event-loop thread on first use."""
        if self._loop is not None and self._loop.is_running():
            return self._loop
        loop = asyncio.new_event_loop()

        def _run():
            self._loop_thread_id = threading.get_ident()
            loop.run_forever()

        threading.Thread(target=_run, daemon=True, name="dbus-native").start()
        self._loop = loop
        return loop

    def _call_on_loop(self, async_fn, timeout: float, *, timing=None, existing_loop=None):
        """Run a coroutine factory on the loop, cross-thread safe.

        Submits ``async_fn()`` onto the dedicated event-loop thread and waits up
        to ``timeout`` seconds for the result. Safe to call from any thread -
        including the loop thread itself (a self-``run_coroutine_threadsafe``
        would deadlock the loop, see the 2026-08-27 wedge). Returns the
        coroutine's result, or None when a synchronous self-call is refused.
        Failures propagate so the caller can distinguish a request deadline
        from a broken shared connection.
        """
        loop = existing_loop
        if loop is None:
            if self._loop is None:
                self._ensure_loop()
            loop = self._loop
        if self._loop_thread_id == threading.get_ident():
            # Already on the loop thread. It is running (run_forever), so a
            # synchronous wait is impossible here. Do not schedule a command
            # whose acceptance we cannot report to the caller.
            self._debug(
                "native_self_call_refused",
                "Native D-Bus synchronous call refused on its event-loop thread",
            )
            return None
        deadline = time.monotonic() + timeout

        async def _run():
            if timing is not None:
                timing["dispatched_at"] = time.monotonic()
                timing["loop_native_tid"] = threading.get_native_id()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # A busy loop must not send an old queued setpoint after the
                # caller has already timed out and requested a safety zero.
                raise TimeoutError("Request expired before dispatch")
            async with asyncio.timeout(remaining):
                if timing is not None:
                    timing["await_started_at"] = time.monotonic()
                try:
                    return await async_fn()
                finally:
                    if timing is not None:
                        timing["completed_at"] = time.monotonic()

        coroutine = _run()
        try:
            if timing is not None:
                timing["submitted_at"] = time.monotonic()
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except BaseException as error:
            coroutine.close()
            if isinstance(error, RuntimeError):
                # Submission failed locally; an endpoint RuntimeError raised
                # later by future.result() must remain request-scoped.
                raise ConnectionError("Native D-Bus event-loop submission failed") from error
            raise
        try:
            return future.result(timeout)
        except BaseException:
            # Also cancel when the control-cycle watchdog interrupts this
            # synchronous wait. Cancellation cannot recall a wire message.
            future.cancel()
            raise
        finally:
            if timing is not None:
                timing["returned_at"] = time.monotonic()

    def _record_write_timing(self, timing, bus, message):
        # The cancelled loop coroutine may finish later. Snapshot once, and
        # report unavailable phases as None rather than inventing durations.
        sampled = dict(timing)
        returned = sampled.setdefault("returned_at", time.monotonic())
        total_ms = (returned - sampled["started_at"]) * 1000.0
        if total_ms < SLOW_SET_TIMING_MS:
            return

        def elapsed(start, finish):
            a, b = sampled.get(start), sampled.get(finish)
            if a is None or b is None or b < a or b > returned:
                return None
            return round((b - a) * 1000.0, 3)

        anchors = {
            key: sampled.get(key)
            for key in (
                "started_at",
                "submitted_at",
                "dispatched_at",
                "await_started_at",
                "call_started_at",
                "send_started_at",
                "send_returned_at",
                "send_done_observed_at",
                "reply_observed_at",
                "call_finished_at",
                "completed_at",
                "returned_at",
            )
        }
        sample = {
            "phase": "native_call",
            "clock": "monotonic",
            "pid": os.getpid(),
            "caller_native_tid": sampled.get("caller_native_tid"),
            "loop_native_tid": sampled.get("loop_native_tid"),
            "anchors": anchors,
            "sender": getattr(bus, "unique_name", None),
            "serial": message.serial or None,
            "total_ms": round(total_ms, 3),
            "setup_ms": elapsed("started_at", "submitted_at"),
            "dispatch_ms": elapsed("submitted_at", "dispatched_at"),
            "await_reply_ms": elapsed("await_started_at", "completed_at"),
            "caller_wakeup_ms": elapsed("completed_at", "returned_at"),
            "call_to_reply_observer_ms": elapsed("call_started_at", "reply_observed_at"),
            "reply_observer_to_resume_ms": elapsed("reply_observed_at", "call_finished_at"),
            "send_sync_ms": elapsed("send_started_at", "send_returned_at"),
            "send_return_to_done_observer_ms": elapsed("send_returned_at", "send_done_observed_at"),
            "send_future_cancelled": sampled.get("send_future_cancelled"),
        }
        # A background sink must never delay the synchronous write caller.
        if not self._write_timings_lock.acquire(blocking=False):
            return
        try:
            self._write_timings.append(sample)
        finally:
            self._write_timings_lock.release()

    def _queue_send_provenance(self, refs):
        if not self._write_timings_lock.acquire(blocking=False):
            return
        try:
            if not self._send_provenance_closed:
                self._send_provenance.append(refs)
        finally:
            self._write_timings_lock.release()

    def drain_write_timings(self) -> list[dict]:
        """Take slow-write diagnostics for a background sink; contains no values."""
        with self._write_timings_lock:
            samples = list(self._write_timings)
            self._write_timings.clear()
            provenance = list(self._send_provenance)
            self._send_provenance.clear()
        for refs in provenance:
            try:
                samples.append(_serialize_send_provenance(refs))
            except Exception:
                # A failed background diagnostic must not disrupt the sink.
                pass
        return samples

    async def _call_message(self, bus, message, *, timing=None):
        """Call once, observing replies without consuming them or replacing call()."""
        observer = None
        stop_send_observer = None
        if timing is not None:
            if self._observe_write_send:
                try:
                    stop_send_observer = bus.observe_send(message, timing)
                except Exception:
                    # An unavailable observer must not prevent the actual call.
                    pass

            def observe_reply(reply):
                # Public user-space receive hook, BEFORE dbus-fast resolves its
                # pending future. Never consume a message or inspect its body.
                if (
                    message.serial
                    and reply.reply_serial == message.serial
                    and reply.message_type in (MessageType.METHOD_RETURN, MessageType.ERROR)
                    and "reply_observed_at" not in timing
                ):
                    timing["reply_observed_at"] = time.monotonic()

            try:
                bus.add_message_handler(observe_reply)
                observer = observe_reply
            except Exception:  # Diagnostics must not prevent a confirmed write.
                pass
            timing["call_started_at"] = time.monotonic()
        try:
            return await bus.call(message)
        finally:
            if timing is not None:
                timing["call_finished_at"] = time.monotonic()
            if stop_send_observer is not None:
                try:
                    stop_send_observer()
                except Exception:
                    # Preserve the call's result/error if optional cleanup fails.
                    pass
            if observer is not None:
                try:
                    bus.remove_message_handler(observer)
                except Exception:
                    pass
            # dbus-fast 2.21.1 leaves cancelled calls in this public Cython dict
            # until a reply/disconnect. A silent endpoint must not leak one
            # handler per retry on the otherwise healthy shared connection.
            # This runs on the bus loop and only touches this message's serial.
            handlers = getattr(bus, "_method_return_handlers", None)
            if isinstance(handlers, dict):
                handlers.pop(message.serial, None)

    def _submit_on_loop(self, async_fn) -> None:
        """Fire-and-forget a coroutine on the loop (no result wait).

        Guards the self-thread case: `run_coroutine_threadsafe` from the loop
        thread would never execute (the loop is busy), so use ensure_future.
        """
        if self._loop is None:
            self._ensure_loop()
        if self._loop_thread_id == threading.get_ident():
            self._track_task(async_fn())
        else:
            try:
                asyncio.run_coroutine_threadsafe(async_fn(), self._loop)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Native D-Bus background submit failed: %s", e)

    def _track_task(self, coroutine) -> asyncio.Task:
        """Schedule a fire-and-forget coroutine, keeping a reference so GC
        can't collect the task before it finishes (see _tasks)."""
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _connect(self):
        from dbus_fast.aio.message_bus import MessageBus

        bus_type = MessageBus
        if self._observe_write_send:

            class SendTimingBus(_SendTimingBusMixin, MessageBus):
                pass

            bus_type = SendTimingBus

        async def _connect_data():
            bus = await bus_type(bus_address=SYSTEM_BUS_ADDRESS).connect()
            bus.add_message_handler(self._handle_message)
            if self._observe_write_send:
                bus._send_provenance_sink = self._queue_send_provenance
            return bus

        self._stop_send_observations(self._bus)
        self._loop = self._ensure_loop()
        self._bus = self._call_on_loop(_connect_data, CONNECT_TIMEOUT)
        if self._bus is None or not getattr(self._bus, "connected", True):
            raise ConnectionError("System D-Bus connection did not become ready")
        if self._subscriptions:
            # Re-arm match rules; signals don't survive a disconnect
            self._replay_subscriptions()

    def _replay_subscriptions(self):
        """Re-arm match rules after a (re)connect; signals don't survive disconnects."""
        # Bus reattachment gives services new unique names; the old map lies.
        self._sender_service.clear()
        # Snapshot: subscribe_signal can add to _subscriptions concurrently.
        self._armed_subscriptions.clear()
        # Optional display rules are repaired on their owner's worker, never
        # delaying the required rules or a caller reconnecting for control.
        for rule in tuple(self._subscriptions - self._optional_subscriptions):
            try:
                self._send_add_match(rule)
                self._armed_subscriptions.add(rule)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Native D-Bus re-subscribe failed (%s): %s", rule, e)
        if self._subscription_services and self._loop is not None and self._bus is not None:
            self._submit_on_loop(self._refresh_sender_map)
        # Fire the reconnect seeding hook off-thread. It performs blocking
        # get_value() reads (one future.result() wait each) that previously ran
        # inline on the caller - the control cycle - so a slow seeding could
        # burn the whole SIGALRM cycle budget and trip "WATCHDOG: Cycle timeout"
        # mid-reconnect (2026-08-27). Defer it so the reconnect never holds the
        # hot path.
        if self.on_reconnect is not None:
            try:
                threading.Thread(
                    target=self._run_reconnect_hook, daemon=True, name="dbus-reconnect"
                ).start()
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Failed to schedule on_reconnect: %s", e)

    def _run_reconnect_hook(self):
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
            interface=DBUS_DAEMON,
            member="AddMatch",
            body=[rule],
            signature="s",
        )

        bus = self._bus

        def _call():
            return self._call_message(bus, message)

        if self._loop_thread_id == threading.get_ident():
            # On the loop thread (reconnect path) - cannot wait synchronously.
            self._track_task(self._send_add_match_async(message))
            return
        reply = self._call_on_loop(_call, MATCH_TIMEOUT)
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            raise ConnectionError(f"AddMatch rejected: {getattr(reply, 'message_type', reply)}")

    async def _send_add_match_async(self, message):
        reply = await self._bus.call(message)
        if reply.message_type != MessageType.METHOD_RETURN:
            raise ConnectionError(f"AddMatch rejected: {reply.message_type}")

    def is_connected(self) -> bool:
        """Report connection availability without initiating synchronous I/O."""
        return bool(
            self._bus is not None
            and getattr(self._bus, "connected", True)
            and time.time() >= self._fail_until
        )

    def subscriptions_healthy(self) -> bool:
        """Every required control rule must be armed on the current connection."""
        required = self._subscriptions - self._optional_subscriptions
        return self.is_connected() and required.issubset(self._armed_subscriptions)

    def optional_subscriptions_healthy(self) -> bool:
        """Display-only rules cannot affect control health, but still need repair."""
        return self.is_connected() and self._optional_subscriptions.issubset(
            self._armed_subscriptions
        )

    def _get_bus(self):
        """Return a connected bus or None (cooldown active / connect failed)."""
        with self._state_lock:
            if time.time() < self._fail_until:
                return None
            try:
                if self._bus is None or not getattr(self._bus, "connected", True):
                    self._connect()
                return self._bus
            except Exception as e:  # pylint: disable=broad-exception-caught
                self._debug(
                    "native_connect_failed",
                    "Native D-Bus connect failed (%s): %s",
                    type(e).__name__,
                    e,
                    error_type=type(e).__name__,
                )
                self._fail_until = time.time() + RECONNECT_COOLDOWN
                self._stop_send_observations(self._bus)
                self._bus = None
                return None

    @staticmethod
    def _stop_send_observations(bus):
        if isinstance(bus, _SendTimingBusMixin):
            try:
                bus.stop_observing_sends()
            except Exception:
                # Optional diagnostics cannot change connection teardown.
                pass

    def _try_disconnect(self, bus, loop) -> None:
        """Best-effort, bounded bus disconnect that never raises.

        dbus_fast's disconnect() is a coroutine only while the bus is open; a
        second call on a torn-down bus returns a non-coroutine and made
        `run_coroutine_threadsafe` raise "A coroutine object is required"
        (seen 2026-08-27). Guard against it and never block the caller long.
        """
        if bus is None or loop is None or not loop.is_running():
            return
        try:
            coro = bus.disconnect()
            if asyncio.iscoroutine(coro):
                asyncio.run_coroutine_threadsafe(coro, loop).result(0.2)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._debug(
                "native_disconnect_failed",
                "Native D-Bus disconnect failed: %s",
                e,
                error_type=type(e).__name__,
            )

    def _mark_failure(self, failed_bus):
        """Drop only the connection that failed, never a newer replacement."""
        with self._state_lock:
            self._stop_send_observations(failed_bus)
            if self._bus is not failed_bus:
                return
            self._fail_until = time.time() + RECONNECT_COOLDOWN
            bus, self._bus = self._bus, None
        if bus is not None:
            self._try_disconnect(bus, self._loop)

    def close(self):
        """Stop the event-loop thread and release the connection."""
        with self._write_timings_lock:
            self._send_provenance_closed = True
            self._send_provenance.clear()
        with self._state_lock:
            self._stop_send_observations(self._bus)
            bus, self._bus = self._bus, None
            self._fail_until = float("inf")
            loop, self._loop = self._loop, None
        if bus is not None:
            self._try_disconnect(bus, loop)
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
        timeout: float = 0.5,
    ):
        """Call a com.victronenergy.BusItem method; reply Message or None."""
        if not _DBUS_FAST_AVAILABLE or self._loop_thread_id == threading.get_ident():
            # A synchronous loop-thread caller cannot await acceptance. Avoid
            # even acquiring the connection lock: a reconnect may hold it while
            # waiting for this loop to arm signal matches.
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
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Invalid destinations, paths or payloads are local caller errors,
            # not evidence that the shared system-bus connection is broken.
            self._debug(
                "native_request_invalid",
                "Invalid native D-Bus request %s %s/%s: %s",
                service,
                member,
                path,
                e,
                service=service,
                path=path,
                error_type=type(e).__name__,
            )
            return None

        timing = (
            {"started_at": time.monotonic(), "caller_native_tid": threading.get_native_id()}
            if member == "SetValue"
            else None
        )
        bus = self._get_bus()
        if bus is None:
            return None
        try:

            def _call():
                return self._call_message(bus, message, timing=timing)

            if timing is None:
                reply = self._call_on_loop(_call, timeout)
            else:
                reply = self._call_on_loop(_call, timeout, timing=timing)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self._debug(
                "native_request_failed",
                "Native D-Bus %s %s/%s failed (%s): %s",
                service,
                member,
                path,
                type(e).__name__,
                e,
                service=service,
                path=path,
                error_type=type(e).__name__,
            )
            # A remote request deadline says nothing about other services or
            # installed signal matches. TimeoutError is also an OSError, so
            # exclude it explicitly from socket/connection failures.
            if isinstance(e, (ConnectionError, EOFError, OSError)) and not isinstance(
                e, TimeoutError
            ):
                self._mark_failure(bus)
            reply = None
        finally:
            if timing is not None:
                self._record_write_timing(timing, bus, message)
        if reply is None:
            if (
                not getattr(bus, "connected", True)
                or self._loop is None
                or not self._loop.is_running()
            ):
                self._mark_failure(bus)
            return None
        if reply.message_type != MessageType.METHOD_RETURN:
            self._debug(
                "native_reply_rejected",
                "D-Bus %s %s returned %s %s",
                service,
                path,
                getattr(reply, "error_name", None),
                reply.body,
                service=service,
                path=path,
            )
            return None
        return reply

    def get_value(self, service: str, path: str, timeout: float = 0.5) -> str | None:
        """GetValue as string (same shape the dbus-send literal parse produced)."""
        reply = self.call_busitem(service, path, "GetValue", timeout=timeout)
        if reply is None or not reply.body:
            return None
        value = getattr(reply.body[0], "value", None)
        return _format_value(value)

    def get_values(self, service: str, timeout: float = 0.5) -> dict[str, str | None] | None:
        """Read one root BusItem snapshot so related fields share a reply."""
        reply = self.call_busitem(service, "/", "GetValue", timeout=timeout)
        return self._format_tree_reply(reply)

    def _read_connected(self, service: str, path: str, timeout: float):
        """Read only the captured live connection; never reconnect or wait on its lock.

        Reconciliation reserves time for a CLI fallback. Connection setup and
        subscription replay must not consume that request budget, including
        when another thread disconnects or reconnects between check and use.
        """
        deadline = time.monotonic() + timeout
        if (
            not _DBUS_FAST_AVAILABLE
            or self._loop_thread_id == threading.get_ident()
            or not self._state_lock.acquire(blocking=False)
        ):
            return None
        try:
            bus, loop = self._bus, self._loop
            if (
                bus is None
                or not getattr(bus, "connected", True)
                or loop is None
                or not loop.is_running()
                or time.time() < self._fail_until
            ):
                return None
        finally:
            self._state_lock.release()
        from dbus_fast import Message

        try:
            message = Message(
                destination=service, path=path, interface=BUSITEM_INTERFACE, member="GetValue"
            )

            async def read():
                if self._bus is not bus or self._loop is not loop or not bus.connected:
                    return None
                return await self._call_message(bus, message)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            reply = self._call_on_loop(read, remaining, existing_loop=loop)
        except Exception:
            # No _mark_failure here: it can wait behind a concurrent reconnect.
            # Normal connection management owns repair; this read just falls back.
            return None
        return (
            reply if reply is not None and reply.message_type == MessageType.METHOD_RETURN else None
        )

    def get_value_connected(self, service: str, path: str, timeout: float = 0.25) -> str | None:
        """Bounded read on an existing connection, without connection setup or repair."""
        reply = self._read_connected(service, path, timeout)
        if reply is None or not reply.body:
            return None
        return _format_value(getattr(reply.body[0], "value", None))

    def get_values_connected(
        self, service: str, timeout: float = 0.25
    ) -> dict[str, str | None] | None:
        """Coherent root snapshot using only an already-connected reader."""
        return self._format_tree_reply(self._read_connected(service, "/", timeout))

    @staticmethod
    def _format_tree_reply(reply) -> dict[str, str | None] | None:
        if reply is None or not reply.body:
            return None
        values = getattr(reply.body[0], "value", reply.body[0])
        if not isinstance(values, dict):
            return None
        return {
            "/" + path.lstrip("/"): _format_value(getattr(value, "value", value))
            for path, value in values.items()
            if isinstance(path, str)
        }

    def get_items_values(self, service: str, timeout: float = 0.5) -> dict[str, object] | None:
        """Read the GetItems snapshot used by aiovelib services."""
        reply = self.call_busitem(service, "/", "GetItems", timeout=timeout)
        if reply is None or not reply.body or not isinstance(reply.body[0], dict):
            return None
        return {
            "/" + path.lstrip("/"): getattr(item.get("Value"), "value", None)
            for path, item in reply.body[0].items()
            if isinstance(path, str) and isinstance(item, dict)
        }

    def set_value(
        self,
        service: str,
        path: str,
        value,
        value_type: str = "int16",
        timeout: float = 0.5,
    ) -> bool:
        """SetValue with an explicitly typed variant. True on success (reply 0)."""
        code = TYPE_CODES.get(value_type)
        if code is None:
            self._debug(
                "native_type_unsupported",
                "Unsupported D-Bus set type: %s",
                value_type,
                value_type=value_type,
            )
            return False
        reply = self.call_busitem(
            service, path, "SetValue", body=[Variant(code, value)], timeout=timeout
        )
        if reply is None:
            return False
        # The facade holds its write lock until the CLI fallback completes;
        # its caller can also hold a watchdog lock. Only queued diagnostics may
        # run on the writer; synchronous handlers could stall a later safety write.
        return not (
            len(reply.body) != 1
            or not isinstance(reply.body[0], int)
            or isinstance(reply.body[0], bool)
            or reply.body[0] != 0
        )

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

    def add_name_owner_handler(self, callback):
        """Register callback(service_name: str, old_owner: str, new_owner: str) for NameOwnerChanged signals."""
        with self._handlers_lock:
            self._name_owner_handlers.append(callback)
        # Remember before the initial connection; _connect replays this rule.
        self._subscriptions.add(NAME_OWNER_RULE)
        if self.is_connected():
            try:
                self._send_add_match(NAME_OWNER_RULE)
                self._armed_subscriptions.add(NAME_OWNER_RULE)
            except Exception as exc:
                logger.debug("NameOwnerChanged subscribe failed: %s", exc)

    def subscribe_signal(
        self, service: str, member: str, path: str, *, required: bool = True
    ) -> bool:
        """Arm one match rule. Idempotent; re-armed automatically after a
        reconnect. Initial values must still be fetched (signals fire on
        change only)."""
        rule = self._build_rule(service, member, path)
        if required:
            # A required caller can upgrade an optional rule, never downgrade it.
            self._optional_subscriptions.discard(rule)
        elif rule not in self._subscriptions:
            self._optional_subscriptions.add(rule)
            self._subscriptions.add(rule)
            self._subscription_services.add(service)
        bus = self._get_bus()
        if bus is None:
            return False
        if rule in self._armed_subscriptions:
            return True
        try:
            self._send_add_match(rule)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Native D-Bus subscribe %s failed: %s", rule, e)
            return False
        self._subscriptions.add(rule)
        self._armed_subscriptions.add(rule)
        self._subscription_services.add(service)
        # Resolve the sender eagerly so the first signals already carry the
        # service tag; lazy refresh below covers services that come up later.
        if self._loop is not None and self._bus is not None:
            try:
                self._submit_on_loop(self._refresh_sender_map)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug("Sender resolve scheduling failed: %s", e)
        return True

    async def _refresh_sender_map(self):
        """Resolve each service once per round, bounded to the captured connection."""
        bus = self._bus
        if bus is None or not getattr(bus, "connected", True) or self._sender_refresh_bus is bus:
            return
        self._sender_refresh_bus = bus
        attempted = set()
        try:
            while self._bus is bus and getattr(bus, "connected", True):
                # New subscriptions can arrive while a daemon reply is pending.
                # Coalesce them into this round without rereading earlier names.
                pending = tuple(self._subscription_services - attempted)
                if not pending:
                    break
                for service in pending:
                    if self._bus is not bus or not getattr(bus, "connected", True):
                        return
                    attempted.add(service)
                    await self._resolve_sender(bus, service)
        finally:
            if self._sender_refresh_bus is bus:
                self._sender_refresh_bus = None

    async def _resolve_sender(self, bus, service):
        """Publish a bounded daemon reply only while its connection/owner is current."""
        from dbus_fast import Message

        owner_version = self._sender_owner_versions.get(service, 0)
        message = Message(
            destination=DBUS_DAEMON,
            path=DBUS_DAEMON_PATH,
            interface=DBUS_DAEMON,
            member="GetNameOwner",
            body=[service],
            signature="s",
        )
        try:
            # A daemon timeout cannot retain a task/pending handler
            # forever or tear down a healthy shared connection.
            async with asyncio.timeout(MATCH_TIMEOUT):
                reply = await self._call_message(bus, message)
        except Exception as error:
            logger.debug("GetNameOwner %s failed: %s", service, error)
            return
        if (
            self._bus is not bus
            or not getattr(bus, "connected", True)
            or self._sender_owner_versions.get(service, 0) != owner_version
        ):
            return
        if reply is not None and reply.message_type == MessageType.METHOD_RETURN and reply.body:
            # Replies queued before a NameOwnerChanged or reconnect
            # must not restore an obsolete service-to-sender binding.
            for sender, known_service in tuple(self._sender_service.items()):
                if known_service == service:
                    self._sender_service.pop(sender, None)
            self._sender_service[str(reply.body[0])] = service

    def subscribe_busitem(self, service: str, path: str, *, required: bool = True) -> bool:
        """Forward per-item PropertiesChanged for one BusItem object."""
        return self.subscribe_signal(service, "PropertiesChanged", path, required=required)

    def subscribe_service_items(self, service: str, *, required: bool = True) -> bool:
        """Forward the bulk ItemsChanged signal a service emits on '/'."""
        return self.subscribe_signal(service, "ItemsChanged", "/", required=required)

    def _handle_message(self, message):
        """Dispatch BusItem change signals to registered handlers.

        Runs on the event-loop thread; handlers are called inline and must be
        quick (they update caches only). Handlers receive the sender's
        well-known service name so path collisions between services (vebus vs
        battery both publish /Dc/0/*) can be routed correctly.
        """
        try:
            if message.message_type != MessageType.SIGNAL:
                return
            if message.interface == BUSITEM_INTERFACE:
                self._handle_busitem_message(message)
            elif message.interface == DBUS_DAEMON and message.member == "NameOwnerChanged":
                self._handle_name_owner_changed(message)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("Native D-Bus signal dispatch failed: %s", e)

    def _handle_busitem_message(self, message):
        sender = getattr(message, "sender", None)
        service = None
        if sender is not None:
            service = self._sender_service.get(sender)
            if service is None:
                self._handle_unresolved_sender(sender)
                return
        else:
            service = None

        if message.member != "ItemsChanged" or message.path != "/":
            if message.member != "PropertiesChanged":
                return
            # PropertiesChanged
            props = message.body[0] if message.body else {}
            self._dispatch(message.path, props, service)
            return

        # ItemsChanged with path "/"
        items = message.body[0] if message.body else {}
        for obj_path, props in items.items():
            self._dispatch(obj_path, props, service)

    def _handle_unresolved_sender(self, _sender: str):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # A signal callback outside its bus loop cannot schedule discovery.
            # Refuse before creating a coroutine or consuming the retry window.
            return
        now = time.monotonic()
        if now < self._sender_refresh_after:
            return
        # Also rate-limit unresolved names which the subscribed services do not
        # own. A burst of distinct unique names must have a fixed task budget.
        self._sender_refresh_after = now + MATCH_TIMEOUT
        self._track_task(self._refresh_sender_map())

    def _handle_name_owner_changed(self, message):
        if len(message.body) >= 3:
            service_name, old_owner, new_owner = map(str, message.body[:3])
            if service_name in self._subscription_services:
                self._sender_owner_versions[service_name] = (
                    self._sender_owner_versions.get(service_name, 0) + 1
                )
            if old_owner:
                self._sender_service.pop(old_owner, None)
            if new_owner and service_name in self._subscription_services:
                self._sender_service[new_owner] = service_name
            with self._handlers_lock:
                handlers = list(self._name_owner_handlers)
            for callback in handlers:
                callback(service_name, old_owner, new_owner)

    def _dispatch(self, path: str, props, service: str | None):
        if "Value" not in props:
            return  # Text-only changes do not invalidate the numeric value.
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

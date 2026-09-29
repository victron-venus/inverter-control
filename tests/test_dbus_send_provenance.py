"""Bounded, socket-free provenance of the actual writer send implementation."""

import asyncio
import hashlib
import marshal
import sys
import threading
import types
import weakref
from unittest.mock import Mock

import pytest

from inverter_control import dbus_native as native
from tests.test_dbus_send_observer import make_bus, message


def observed_send(bus):
    request = message()
    stop = bus.observe_send(request, {})
    try:
        return bus.send(request)
    finally:
        stop()


def captured_refs():
    async def run():
        bus = make_bus(immediate=True)
        samples = []
        bus._send_provenance_sink = samples.append
        observed_send(bus)
        return samples[0]

    return asyncio.run(run())


@pytest.mark.parametrize("immediate", [False, True])
def test_actual_transport_once_identical_future_and_first_observed_send_only(immediate):
    async def run():
        bus = make_bus(immediate=immediate)
        client = native.NativeDbusClient(observe_write_send=True)
        bus._send_provenance_sink = client._queue_send_provenance
        unrelated = bus.send(message())
        assert not client._send_provenance
        first = observed_send(bus)
        second = observed_send(bus)
        assert [unrelated, first, second] == [entry[1] for entry in bus._writer.calls]
        assert first is bus._writer.calls[1][1]
        assert len(bus._writer.calls) == 3
        assert len(client._send_provenance) == 1
        refs = client._send_provenance[0]
        assert refs["bus_mro"][0] is type(bus)
        assert refs["writer_mro"][0] is type(bus._writer)
        assert refs["future_mro"][0] is type(first)
        assert refs["sender"] == ":1.42"
        assert refs["send_returned"] is True
        assert refs["send_qualname"].endswith("MessageBus.send")
        assert not any(value is bus or value is first for value in refs.values())
        assert first.done() is immediate
        client.close()
        assert not client._send_provenance
        assert first.done() is immediate

    asyncio.run(run())


def test_each_replacement_connection_can_supply_one_record():
    async def run():
        client = native.NativeDbusClient(observe_write_send=True)
        for index in range(6):
            bus = make_bus(immediate=True)
            bus.unique_name = f":1.{index}"
            bus._send_provenance_sink = client._queue_send_provenance
            observed_send(bus)
            observed_send(bus)
        assert client._send_provenance.maxlen == 4
        assert [sample["sender"] for sample in client._send_provenance] == [
            ":1.2",
            ":1.3",
            ":1.4",
            ":1.5",
        ]
        client.close()

    asyncio.run(run())


def test_capture_uses_actual_bound_callable_code_before_exact_invocation():
    class Transport:
        def send(self, _message):
            self.calls += 1
            self.original.__code__ = self.replacement.__code__
            return self.future

        def replacement(self, _message):
            raise AssertionError("replacement must not execute")

    old_code = Transport.send.__code__
    Transport.original = staticmethod(Transport.send)
    Transport.replacement = staticmethod(Transport.replacement)

    class Bus(native._SendTimingBusMixin, Transport):
        pass

    async def run():
        bus = Bus()
        bus._writer = object()
        bus.unique_name = ":1.99"
        bus.calls = 0
        bus.future = asyncio.get_running_loop().create_future()
        samples = []
        bus._send_provenance_sink = samples.append
        future = observed_send(bus)
        assert future is bus.future
        assert bus.calls == 1
        assert samples[0]["send_code"] is old_code
        assert Transport.send.__code__ is Transport.replacement.__code__
        code = native._serialize_send_provenance(samples[0])["underlying_send"]["code"]
        assert code["sha256"] == hashlib.sha256(marshal.dumps(old_code, 4)).hexdigest()
        assert (
            code["sha256"] != hashlib.sha256(marshal.dumps(Transport.send.__code__, 4)).hexdigest()
        )
        assert not future.done()

    asyncio.run(run())


def test_original_error_and_failed_diagnostic_sink_cannot_change_transport():
    async def run():
        failure = ValueError("private command body must never enter provenance")
        for sink in ([].append, Mock(side_effect=RuntimeError("diagnostic failure"))):
            bus = make_bus(error=failure)
            bus._send_provenance_sink = sink
            with pytest.raises(ValueError) as caught:
                observed_send(bus)
            assert caught.value is failure
            assert len(bus._writer.calls) == 1
            if isinstance(sink, Mock):
                refs = sink.call_args.args[0]
            else:
                refs = sink.__self__[0]
            record = native._serialize_send_provenance(refs)
            assert record["send_returned"] is False
            assert record["future"]["class"] is None
            assert "private command body" not in repr(record)

    asyncio.run(run())


def test_send_does_not_open_hash_or_serialize_and_drain_releases_lock(monkeypatch):
    async def run():
        bus = make_bus(immediate=True)
        client = native.NativeDbusClient(observe_write_send=True)
        bus._send_provenance_sink = client._queue_send_provenance
        serializer = native._serialize_send_provenance
        with monkeypatch.context() as patch:
            for owner, name in (
                (native.os, "open"),
                (native.hashlib, "sha256"),
                (native, "_serialize_send_provenance"),
            ):
                patch.setattr(owner, name, Mock(side_effect=AssertionError("hot path I/O")))
            observed_send(bus)
            assert len(client._send_provenance) == 1
            native.os.open.assert_not_called()
            native.hashlib.sha256.assert_not_called()
            native._serialize_send_provenance.assert_not_called()

        def outside_lock(refs):
            assert client._write_timings_lock.acquire(blocking=False)
            client._write_timings_lock.release()
            assert threading.get_ident() != writer_thread
            return serializer(refs)

        writer_thread = threading.get_ident()
        monkeypatch.setattr(native, "_serialize_send_provenance", outside_lock)
        records = await asyncio.to_thread(client.drain_write_timings)
        assert len(records) == 1
        assert records[0]["schema"] == native.PROVENANCE_SCHEMA
        assert records[0]["phase"] == "writer_provenance"
        assert not client._send_provenance
        assert client.drain_write_timings() == []

    asyncio.run(run())


def test_busy_queue_never_waits_or_retries_first_connection_capture():
    async def run():
        bus = make_bus(immediate=True)
        client = native.NativeDbusClient(observe_write_send=True)
        bus._send_provenance_sink = client._queue_send_provenance
        assert client._write_timings_lock.acquire(blocking=False)
        try:
            observed_send(bus)
            assert not client._send_provenance
        finally:
            client._write_timings_lock.release()
        observed_send(bus)
        assert not client._send_provenance
        assert len(bus._writer.calls) == 2

    asyncio.run(run())


def test_close_during_underlying_send_rejects_late_refs_without_cancelling_future():
    async def run():
        bus = make_bus()
        client = native.NativeDbusClient(observe_write_send=True)
        bus._send_provenance_sink = client._queue_send_provenance
        original = bus._writer.schedule_write

        def close_during_send(request, future):
            original(request, future)
            client.close()

        bus._writer.schedule_write = close_during_send
        future = observed_send(bus)
        assert not future.done()
        assert len(bus._writer.calls) == 1
        assert client._send_provenance_closed
        assert not client._send_provenance
        client._queue_send_provenance({"late": True})
        assert not client._send_provenance

    asyncio.run(run())


def test_queued_type_references_released_on_close():
    client = native.NativeDbusClient(observe_write_send=True)
    cls = type("EphemeralDiagnosticType", (), {})
    reference = weakref.ref(cls)
    client._queue_send_provenance({"bus_mro": (cls,)})
    del cls
    assert reference() is not None
    client.close()
    import gc

    gc.collect()
    assert reference() is None


def test_stopped_bus_releases_sink_and_cannot_capture_again():
    async def run():
        bus = make_bus(immediate=True)
        sink = Mock()
        bus._send_provenance_sink = sink
        bus.stop_observing_sends()
        await asyncio.sleep(0)
        assert bus._send_provenance_sink is None
        assert bus.observe_send(message(), {}) is None
        assert bus.send(message()) is bus._writer.calls[0][1]
        sink.assert_not_called()

    asyncio.run(run())


def test_serializer_failure_is_dropped_and_releases_queue(monkeypatch):
    client = native.NativeDbusClient(observe_write_send=True)
    client._queue_send_provenance(captured_refs())
    client._write_timings.append({"phase": "native"})
    monkeypatch.setattr(
        native, "_serialize_send_provenance", Mock(side_effect=ValueError("private"))
    )
    assert client.drain_write_timings() == [{"phase": "native"}]
    assert not client._send_provenance


def test_module_hash_is_current_file_only_and_uses_allowlist(tmp_path, monkeypatch):
    path = tmp_path / "module.so"
    content = b"current file bytes, not a loaded memory image"
    path.write_bytes(content)
    module = types.ModuleType("dbus_fast._provenance_fixture")
    module.__file__ = str(path)
    module.__spec__ = types.SimpleNamespace(origin=str(path))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    budget = [len(content)]
    result = native._provenance_module(module.__name__, budget)
    assert result["status"] == "current_file_hashed"
    assert result["loaded_bytes_verified"] is False
    assert result["current_file_sha256"] == hashlib.sha256(content).hexdigest()
    assert result["spec_origin"] == str(path)
    assert budget == [0]
    with monkeypatch.context() as patch:
        patch.setattr(native.os, "open", Mock(side_effect=AssertionError("not allowlisted")))
        assert (
            native._provenance_module("secret_application", [100])["status"]
            == "module_not_allowlisted"
        )
        native.os.open.assert_not_called()
    assert native._provenance_module("dbus_fast.not_loaded", [100])["status"] == "module_not_loaded"
    assert native._provenance_module("builtins", [100])["status"] == "no_bounded_file_path"


@pytest.mark.parametrize("kind", ["large", "fifo", "directory", "missing", "growing", "shrinking"])
def test_module_read_bound_and_failure_status(tmp_path, monkeypatch, kind):
    path = tmp_path / "module.so"
    if kind == "fifo":
        native.os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind != "missing":
        path.write_bytes(b"before")
    module = types.ModuleType("dbus_fast._provenance_fixture")
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    actual_read = native.os.read
    read_sizes = []

    def changed_read(fd, size):
        read_sizes.append(size)
        if kind == "growing":
            path.write_bytes(b"before more")
        elif kind == "shrinking":
            path.write_bytes(b"x")
        return actual_read(fd, size)

    monkeypatch.setattr(native.os, "read", changed_read)
    budget = [3 if kind == "large" else 6]
    result = native._provenance_module(module.__name__, budget)
    expected = {
        "large": "file_budget_exceeded",
        "fifo": "not_regular_file",
        "directory": "not_regular_file",
        "missing": "file_unavailable",
        "growing": "file_changed_during_read",
        "shrinking": "file_changed_during_read",
    }
    assert result["status"] == expected[kind]
    assert budget[0] >= 0
    assert sum(read_sizes) <= 6 if kind != "shrinking" else True
    assert "current_file_sha256" not in result
    if kind == "missing":
        assert result["error_type"] == "FileNotFoundError"
        assert "error" not in result


def test_python_code_budget_and_compiled_callable_are_explicit():
    assert native._provenance_code(None) == {"status": "no_python_code"}
    assert native._provenance_code(getattr(len, "__code__", None))["status"] == "no_python_code"
    code = (lambda: None).__code__
    assert native._provenance_code(code)["status"] == "captured_python_code"
    huge = code.replace(co_consts=(b"x" * 262145,))
    assert native._provenance_code(huge) == {"status": "code_budget_exceeded"}
    many = code.replace(co_consts=tuple(range(1025)))
    assert native._provenance_code(many) == {"status": "code_budget_exceeded"}


def test_schema_bounds_allowlist_and_log_payload_limit(monkeypatch):
    refs = captured_refs()
    refs["sender"] = "s" * 1024
    refs["unused_body"] = "PRIVATE_DBUS_PAYLOAD_SENTINEL"
    record = native._serialize_send_provenance(refs)
    assert set(record) == {
        "phase",
        "schema",
        "clock",
        "observed_at",
        "pid",
        "loop_native_tid",
        "sender",
        "send_returned",
        "underlying_send",
        "bus",
        "writer",
        "future",
        "modules",
        "modules_truncated",
    }
    assert len(record["sender"]) == 128
    assert len(record["modules"]) <= 8
    assert all(len(record[role]["mro"]) <= 8 for role in ("bus", "writer", "future"))
    assert "unused_body" not in repr(record)
    assert "PRIVATE_DBUS_PAYLOAD_SENTINEL" not in repr(record)
    assert len(repr(record).encode("utf-8")) <= 32768
    monkeypatch.setattr(native, "PROVENANCE_RECORD_LIMIT", 100)
    with pytest.raises(ValueError, match="record budget"):
        native._serialize_send_provenance(refs)


def test_capture_limits_deep_mro_and_oversize_unicode_record_is_dropped():
    async def run():
        parent = type("Base", (), {})
        for index in range(12):
            parent = type(
                "\U0001f4a1" * 256, (parent,), {"__module__": "\U0001f512" * 256 + str(index)}
            )
        bus = make_bus(immediate=True)
        bus._writer.__class__ = type("Writer", (type(bus._writer), parent), {})
        client = native.NativeDbusClient(observe_write_send=True)
        bus._send_provenance_sink = client._queue_send_provenance
        observed_send(bus)
        refs = client._send_provenance[0]
        assert len(refs["writer_mro"]) == 8
        assert refs["writer_truncated"] is True
        # Repeat the bounded but maximally expanded descriptors in all roles.
        refs["bus_mro"] = refs["future_mro"] = refs["writer_mro"]
        records = client.drain_write_timings()
        assert records == []
        assert not client._send_provenance

    asyncio.run(run())


def test_failed_partial_module_read_still_consumes_shared_byte_budget(tmp_path, monkeypatch):
    path = tmp_path / "module.so"
    path.write_bytes(b"abcdef")
    module = types.ModuleType("dbus_fast._provenance_fixture")
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    read = Mock(side_effect=[b"ab", OSError("private filesystem details")])
    monkeypatch.setattr(native.os, "read", read)
    budget = [6]
    first = native._provenance_module(module.__name__, budget)
    assert first["status"] == "file_unavailable"
    assert budget == [4]
    assert "private filesystem" not in repr(first)
    second = native._provenance_module(module.__name__, budget)
    assert second["status"] == "file_budget_exceeded"
    assert read.call_count == 2

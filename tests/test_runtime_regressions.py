"""Retrospective regressions for documented August 2026 control-loop failures."""

import errno
import threading
from unittest.mock import Mock

import pytest

import main
from inverter_control.victron import VictronDBus


@pytest.mark.parametrize(
    "error", [BrokenPipeError(errno.EPIPE, "closed"), BlockingIOError(errno.EAGAIN, "full")]
)
def test_log_pipe_backpressure_does_not_abort_the_control_caller(error):
    """A failed diagnostic write must not interrupt the next controller action."""
    underlying = Mock()
    underlying.write.side_effect = [error, 4]
    stream = main._BrokenPipeSafeStream(underlying)
    written = stream.write("status")
    assert written == len("status")
    # Suppression is specific to this failed write; a recovered pipe is reused.
    written = stream.write("next")
    assert written == 4
    assert underlying.write.call_count == 2


def test_log_pipe_does_not_hide_unrelated_io_errors():
    underlying = Mock()
    underlying.write.side_effect = OSError(errno.ENOSPC, "disk full")
    with pytest.raises(OSError) as caught:
        main._BrokenPipeSafeStream(underlying).write("status")
    assert caught.value.errno == errno.ENOSPC


def test_overlapping_discovery_skips_instead_of_queuing_another_process():
    """Startup/signal/poll races must not pile up expensive discovery subprocesses."""
    device = object.__new__(VictronDBus)
    device._discovery_lock = threading.Lock()
    device._native = None
    device._apply_discovery = Mock()
    device._log_discovery_failure = Mock()
    entered, release, second_done = threading.Event(), threading.Event(), threading.Event()

    def command():
        entered.set()
        if not release.wait(2):
            raise TimeoutError("fixture discovery release")
        return Mock(stdout="com.victronenergy.fixture")

    device._run_discovery_command = Mock(side_effect=command)
    first = threading.Thread(target=device._discover_services)

    def second_call():
        try:
            device._discover_services()
        finally:
            second_done.set()

    second = threading.Thread(target=second_call)
    first.start()
    try:
        assert entered.wait(1)
        second.start()
        assert second_done.wait(0.3), "discovery queued behind an in-flight subprocess"
        assert device._run_discovery_command.call_count == 1
        device._apply_discovery.assert_not_called()
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    device._apply_discovery.assert_called_once_with("com.victronenergy.fixture")
    device._log_discovery_failure.assert_not_called()
    # The lock is released after completion, so discovery remains usable.
    device._discover_services()
    assert device._run_discovery_command.call_count == 2


@pytest.mark.parametrize(
    "reply,expected",
    [("variant uint32 3", (3, "Bulk")), ("variant int32 0\n", (0, "Off")), ("9", (9, "Inverting"))],
)
def test_inverter_state_accepts_actual_dbus_literal_replies(reply, expected):
    """Parsing the full literal with int() used to fail every state poll."""
    assert VictronDBus._parse_inverter_state_code(reply) == expected


@pytest.mark.parametrize("reply", ["", "variant uint32 invalid"])
def test_inverter_state_rejects_unparseable_replies(reply):
    with pytest.raises(ValueError):
        VictronDBus._parse_inverter_state_code(reply)


def test_smartshunt_selection_uses_product_not_mutable_service_suffix():
    """A chain/system aggregate must not replace the whole-bank shunt meter."""
    device = object.__new__(VictronDBus)
    device._vebus_service = None
    device._shunt_service = None
    device._shunt_lock = threading.RLock()
    device._clear_shunt_data = Mock()
    device._log_service_changes = Mock()
    names = {
        "com.victronenergy.battery.ttyUSB4": "JBD battery chain",
        "com.victronenergy.battery.ttyUSB9": "SmartShunt 500A/50mV",
        "com.victronenergy.battery.virtual": "Virtual Battery",
    }
    device._read_product_name = Mock(side_effect=names.get)
    device._apply_discovery("\n".join(names))
    assert device._shunt_service == "com.victronenergy.battery.ttyUSB9"
    device._clear_shunt_data.assert_called_once()
    # The same physical meter receives a different serial suffix after reboot.
    names["com.victronenergy.battery.ttyUSB2"] = names.pop("com.victronenergy.battery.ttyUSB9")
    device._apply_discovery("\n".join(names))
    assert device._shunt_service == "com.victronenergy.battery.ttyUSB2"
    assert device._clear_shunt_data.call_count == 2
    names.pop("com.victronenergy.battery.ttyUSB2")
    device._apply_discovery("\n".join(names))
    assert device._shunt_service is None
    assert device._clear_shunt_data.call_count == 3


def test_blocked_console_client_does_not_block_another_producer(monkeypatch):
    """Exercise the actual send lock while the control producer enqueues output."""
    import queue
    from collections import deque

    from inverter_control import console_server

    entered, release, produced = threading.Event(), threading.Event(), threading.Event()
    client = Mock()

    def blocked_send(_data):
        entered.set()
        if not release.wait(2):
            raise TimeoutError("fixture console release")

    client.sendall.side_effect = blocked_send
    monkeypatch.setattr(console_server, "_clients", {client})
    monkeypatch.setattr(console_server, "_clients_lock", threading.Lock())
    monkeypatch.setattr(console_server, "_sender_queue", queue.Queue(maxsize=2))
    monkeypatch.setattr(console_server, "_console_buffer", deque(maxlen=100))
    sender = threading.Thread(target=console_server._send_to_clients, args=("first",))

    def produce():
        console_server.broadcast_line("next control status")
        produced.set()

    producer = threading.Thread(target=produce)
    sender.start()
    try:
        assert entered.wait(1)
        producer.start()
        assert produced.wait(0.3), "console sender blocked the control producer"
        assert console_server._sender_queue.get_nowait() == "next control status"
        assert list(console_server._console_buffer) == ["next control status"]
        assert client.sendall.call_count == 1
    finally:
        release.set()
        sender.join(2)
        if producer.ident is not None:
            producer.join(2)
    assert not sender.is_alive() and not producer.is_alive()


@pytest.mark.parametrize("value,expected", [("7.2 kW", 7.2), ("85 %", 85), ("-50 W", -50)])
def test_ha_numeric_measurement_retains_value_before_unit_suffix(value, expected):
    """Legacy HA sensors supplied units; direct float conversion discarded them."""
    from inverter_control.homeassistant import HomeAssistantClient

    assert HomeAssistantClient._parse_numeric(None, value) == expected


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_legacy_dbus_float_reader_rejects_nonfinite_values(value):
    """A successful D-Bus transport does not make NaN or infinity a measurement."""
    device = object.__new__(VictronDBus)
    device._dbus_get = Mock(return_value=value)
    assert device._get_float("com.victronenergy.fixture", "/Ac/Power") == 0.0

"""Write results and watchdog transitions must not depend on diagnostic sinks."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from dbus_fast import Message, MessageType
from test_write_isolation import facade as facade  # noqa: PLC0414 - re-export pytest fixture

from inverter_control.controller import InverterController
from inverter_control.watchdog import HardwareWatchdog
from inverter_control.write_diagnostics import CAPACITY, MAX_TEXT, WriteDiagnostics

SERVICE = "com.victronenergy.system"


def performance(facade):
    return SimpleNamespace(
        victron=facade, metrics=SimpleNamespace(sample_process=Mock(), snapshot=dict)
    )


@pytest.mark.parametrize("ack,accepted", [("int32 0", True), ("int32 2", False)])
def test_blocked_performance_sink_cannot_hold_outer_hardware_lock(facade, ack, accepted):
    entered, release = threading.Event(), threading.Event()
    writes, results = [], []
    controller = performance(facade)
    watchdog = HardwareWatchdog(facade, diagnostics=facade.write_diagnostics)
    facade._vebus_service = SERVICE

    def native(_service, _path, value, _type):
        writes.append(("native", value))
        return value == 0

    def cli(*_args, **_kwargs):
        writes.append(("cli", 100))
        return ack

    def sink(*_args):
        entered.set()
        assert release.wait(3)

    writer = threading.Thread(
        target=lambda: results.append(watchdog.write_control_setpoint(100, 0))
    )
    consumer = threading.Thread(target=lambda: InverterController._read_performance(controller))
    with (
        patch.object(facade._native_write, "set_value", side_effect=native),
        patch.object(facade, "_safe_subprocess", side_effect=cli),
        patch("inverter_control.controller.logger.log", side_effect=sink),
        patch("inverter_control.controller.prom_metrics_publish"),
    ):
        try:
            writer.start()
            writer.join(1)
            assert not writer.is_alive() and results == [accepted]
            assert watchdog._has_valid_setpoint is accepted
            assert facade._consecutive_errors == (0 if accepted else 1)
            consumer.start()
            assert entered.wait(1)
            assert not facade._set_lock.locked()
            assert watchdog.control_generation() == 0
            # A manual rejection/status transition uses the same outer lock.
            assert watchdog.reject_setpoint_override("fixture")["last_error"] == "fixture"
            assert watchdog.write_control_setpoint(0, 0) is True
            assert writes == [("native", 100), ("cli", 100), ("native", 0)]
            controller.metrics.sample_process.assert_not_called()  # Sink can delay metrics only.
            assert not release.is_set()
        finally:
            release.set()
            writer.join(3)
            if consumer.ident is not None:
                consumer.join(3)
    assert not consumer.is_alive()


def test_buffer_is_bounded_scalar_only_and_drops_under_contention():
    buffer = WriteDiagnostics()

    class Opaque:
        def __str__(self):
            raise AssertionError("must not stringify diagnostic inputs")

    for i in range(CAPACITY + 3):
        buffer.record(
            "native_fallback", path=str(i), service="s" * 1000, error_type=Opaque(), accepted=True
        )
    records = buffer.drain()
    assert len(records) == CAPACITY
    assert [x["path"] for x in records] == [str(i) for i in range(3, CAPACITY + 3)]
    assert all(len(x["service"]) == MAX_TEXT and "error_type" not in x for x in records)
    assert json.loads(json.dumps(records)) == records
    buffer.record(Opaque(), path="invalid")
    buffer.record("arbitrary_unreviewed_event")
    with buffer._lock:
        buffer.record("native_fallback", path="contention")  # Must not wait for itself.
    assert buffer.drain() == []


@pytest.mark.parametrize("queue_failure", [False, True])
def test_full_busy_or_failing_buffer_cannot_change_accepted_write(facade, queue_failure):
    buffer = facade.write_diagnostics
    for _ in range(CAPACITY):
        buffer.record("native_fallback")
    with (
        patch.object(facade._native_write, "set_value", return_value=False),
        patch.object(facade, "_safe_subprocess", return_value="int32 0"),
    ):
        if queue_failure:
            with patch.object(buffer, "record", side_effect=RuntimeError("broken diagnostic")):
                assert facade._dbus_set(SERVICE, "/Setpoint", 100)
        else:
            with buffer._lock:
                assert facade._dbus_set(SERVICE, "/Setpoint", 100)
    assert facade._consecutive_errors == 0
    assert len(buffer.drain()) == CAPACITY


def test_cli_exception_never_formats_or_logs_under_write_lock(facade):
    class Unformattable(OSError):
        def __str__(self):
            raise AssertionError("exception formatting is not allowed on write path")

    with (
        patch.object(facade._native_write, "set_value", return_value=False),
        patch("inverter_control.victron.subprocess.run", side_effect=Unformattable()),
        patch("inverter_control.victron.logger.debug") as debug,
        patch("inverter_control.victron.logger.warning") as warning,
    ):
        assert facade._dbus_set(SERVICE, "/Setpoint", 100) is False
    debug.assert_not_called()
    warning.assert_not_called()
    assert facade.subprocess_calls == 1 and facade._consecutive_errors == 1
    records = facade.drain_write_diagnostics()
    assert [x["event"] for x in records] == [
        "cli_exception",
        "native_fallback",
        "fallback_rejected",
    ]
    assert records[0] == {"event": "cli_exception", "error_type": "Unformattable"}


@pytest.mark.parametrize(
    "failure", ["timeout", "connection", "reply", "invalid", "connect", "unsupported"]
)
def test_actual_native_failure_paths_do_not_execute_logger(facade, failure):
    writer = facade._native_write
    bus = writer._bus
    path, kind = "/Setpoint", "int32"
    if failure == "invalid":
        path = "not/an/object/path"
    if failure == "unsupported":
        kind = "unsupported"
    if failure == "connect":
        writer._bus = None

    class Unformattable(ConnectionError):
        def __str__(self):
            raise AssertionError("raw exception string must never reach a sink here")

    async def response(_message):
        if failure == "timeout":
            raise TimeoutError("private diagnostic detail")
        if failure == "connection":
            raise Unformattable()
        return Message(
            message_type=MessageType.ERROR,
            error_name="fixture.Error",
            reply_serial=1,
            signature="s",
            body=["private reply detail"],
        )

    with (
        patch.object(writer, "_connect", side_effect=Unformattable()),
        patch.object(bus, "call", side_effect=response),
        patch("inverter_control.dbus_native.logger.debug") as debug,
        patch("inverter_control.dbus_native.logger.warning") as warning,
        patch.object(facade, "_safe_subprocess", return_value="int32 0") as cli,
    ):
        assert facade._dbus_set(SERVICE, path, 100, kind) is True
    debug.assert_not_called()
    warning.assert_not_called()
    cli.assert_called_once()
    assert facade._consecutive_errors == 0
    records = facade.drain_write_diagnostics()
    expected = {
        "timeout": "native_request_failed",
        "connection": "native_request_failed",
        "reply": "native_reply_rejected",
        "invalid": "native_request_invalid",
        "connect": "native_connect_failed",
        "unsupported": "native_type_unsupported",
    }[failure]
    assert records[0]["event"] == expected
    assert records[-1]["event"] == "native_fallback"
    assert "private" not in json.dumps(records)


def test_disconnect_exception_and_failed_buffer_do_not_change_native_result(facade):
    writer = facade._native_write
    bus = writer._bus
    with (
        patch.object(bus, "disconnect", side_effect=OSError("private error")),
        patch("inverter_control.dbus_native.logger.debug") as debug,
    ):
        writer._try_disconnect(bus, writer._loop)
    debug.assert_not_called()
    assert facade.drain_write_diagnostics() == [
        {"event": "native_disconnect_failed", "error_type": "OSError"}
    ]
    with (
        patch.object(writer, "_connect", side_effect=OSError()),
        patch.object(facade.write_diagnostics, "record", side_effect=RuntimeError()),
        patch.object(facade, "_safe_subprocess", return_value="int32 0"),
    ):
        writer._bus = None
        assert facade._dbus_set(SERVICE, "/Setpoint", 100)


def test_failing_drain_sink_delays_metrics_but_not_next_command(facade):
    controller = performance(facade)
    with (
        patch.object(facade._native_write, "set_value", return_value=False),
        patch.object(facade, "_safe_subprocess", return_value="int32 0"),
        patch("inverter_control.controller.logger.log", side_effect=RuntimeError("sink failed")),
    ):
        assert facade._dbus_set(SERVICE, "/Setpoint", 100)
        with pytest.raises(RuntimeError, match="sink failed"):
            InverterController._read_performance(controller)
        controller.metrics.sample_process.assert_not_called()
        for _ in range(CAPACITY + 1):
            assert facade._dbus_set(SERVICE, "/Setpoint", 0)
    assert len(facade.drain_write_diagnostics()) == CAPACITY


@pytest.mark.parametrize("action", ["zero", "grid", "manual", "dry", "status_callback"])
@pytest.mark.parametrize("queue_state", ["normal", "full", "busy", "broken"])
def test_global_handler_cannot_delay_watchdog_commands_or_transitions(
    facade, action, queue_state, monkeypatch
):
    import logging
    import time

    class BlockingSink(logging.Handler):
        def __init__(self):
            super().__init__()
            self.entered, self.release_sink = threading.Event(), threading.Event()

        def emit(self, _record):
            self.entered.set()
            assert self.release_sink.wait(3)

    sink = BlockingSink()
    logger = logging.getLogger("isolated-write-diagnostic-fixture")
    monkeypatch.setattr(logger, "level", logging.DEBUG)
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(sink)  # Handler's real lock is shared by all source loggers.
    for module in ("controller", "victron", "dbus_native"):
        monkeypatch.setattr(f"inverter_control.{module}.logger", logger)
    monkeypatch.setattr("inverter_control.controller.prom_metrics_publish", Mock())
    controller = performance(facade)
    facade._vebus_service = SERVICE
    buffer = facade.write_diagnostics
    watchdog = HardwareWatchdog(
        facade,
        get_setpoint=Mock(side_effect=RuntimeError("prior value unavailable")),
        grid_loss_hold_seconds=0.01,
        diagnostics=buffer,
    )
    buffer.record("native_fallback")
    consumer = threading.Thread(target=lambda: InverterController._read_performance(controller))
    done, errors = threading.Event(), []

    def act():
        try:
            if action == "zero":
                watchdog.grid_loss_hold_seconds = None
                watchdog._fail_count = watchdog._fail_threshold - 1
                watchdog._last_dbus_update = watchdog._last_setpoint_update = time.monotonic() - 40
                watchdog._check_heartbeat()
            elif action == "grid":
                watchdog._has_valid_setpoint = True
                watchdog._telemetry_invalid = True
                watchdog._grid_invalid_since = time.monotonic() - 1
                watchdog.check_grid_loss()
            elif action == "manual":
                assert watchdog.set_setpoint_override(150)["value"] == 150
                assert watchdog.set_setpoint_override(None)["value"] is None
                assert watchdog.control_generation() == 2
            elif action == "dry":
                watchdog.dry_run = True
                with watchdog._lock:
                    watchdog._apply_failsafe()
            else:
                watchdog.set_override_status_callback(
                    Mock(side_effect=RuntimeError("status failed"))
                )
            assert watchdog._lock.acquire(blocking=False)
            watchdog._lock.release()
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    actor = threading.Thread(target=act)
    consumer.start()
    try:
        assert sink.entered.wait(1)
        if queue_state == "full":
            for _ in range(CAPACITY):
                buffer.record("native_fallback")
        elif queue_state == "busy":
            buffer._lock.acquire()
        elif queue_state == "broken":
            monkeypatch.setattr(buffer, "record", Mock(side_effect=RuntimeError("buffer failed")))
        actor.start()
        assert done.wait(1), "A real shared logging handler must not hold a hardware transition"
        assert errors == []
        assert not sink.release_sink.is_set()
        values = [message.body[0].value for message in facade._native_write._bus.messages]
        assert (
            values
            == {"zero": [0], "grid": [-10], "manual": [150], "dry": [], "status_callback": []}[
                action
            ]
        )
        assert watchdog._hardware_forced is (action in {"zero", "grid"})
        controller.metrics.sample_process.assert_not_called()
    finally:
        if queue_state == "busy" and buffer._lock.locked():
            buffer._lock.release()
        sink.release_sink.set()
        actor.join(3)
        consumer.join(3)
        logger.removeHandler(sink)
        sink.close()
    assert not actor.is_alive() and not consumer.is_alive()
    assert len(buffer.drain()) <= CAPACITY


def test_controller_explicitly_shares_one_buffer_with_watchdog():
    from test_main import _make_controller

    controller, _, _, _ = _make_controller()
    assert controller._watchdog._diagnostics is controller.victron.write_diagnostics

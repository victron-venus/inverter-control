"""Control diagnostics cannot delay writes or hide failed control iterations."""

import logging
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from test_main import _make_controller

from inverter_control import controller as controller_module
from inverter_control.write_diagnostics import CAPACITY, MAX_TEXT, WriteDiagnostics


@contextmanager
def control_fixture():
    controller, victron, _, _ = _make_controller()
    diagnostics = WriteDiagnostics()
    victron.write_diagnostics = diagnostics
    controller._watchdog._diagnostics = diagnostics
    victron.drain_write_diagnostics.side_effect = diagnostics.drain
    victron.get_system_data.return_value = {"_grid_valid": True, "gt": 0}
    victron.set_grid_setpoint.return_value = True
    controller._last_update_state_time = 0
    with (
        patch.object(controller, "_grid_ready_for_control", return_value=True),
        patch.object(controller, "_update_dvcc_limits"),
        patch.object(controller, "calculate_setpoint", return_value=(-100, "")),
        patch.object(controller, "handle_minimize_charging"),
        patch.object(controller, "update_state"),
        patch.object(controller, "get_control_flag", return_value=False),
        patch.object(controller_module, "broadcast_line"),
    ):
        yield controller, victron, diagnostics


def test_slow_stage_can_write_while_background_logger_holds_shared_handler():
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    errors, results = [], []
    now = [1000.0]

    class BlockedHandler(logging.Handler):
        def emit(self, _record):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("fixture release timeout")

    handler = BlockedHandler()
    logger = controller_module.logger
    with (
        control_fixture() as (controller, victron, diagnostics),
        patch.object(logger, "handlers", [handler]),
        patch.object(logger, "propagate", False),
        patch.object(logger, "level", logging.WARNING),
        patch.object(controller_module.time, "monotonic", side_effect=lambda: now[0]),
        patch.object(controller_module.time, "perf_counter", side_effect=lambda: now[0]),
    ):

        def read_system():
            now[0] += 0.4
            return {"_grid_valid": True, "gt": 0}

        def write(_value):
            written.set()
            return True

        def run():
            try:
                results.append(controller.run_cycle())
            except Exception as error:
                errors.append(error)

        victron.get_system_data.side_effect = read_system
        victron.set_grid_setpoint.side_effect = write
        background = threading.Thread(target=lambda: logger.warning("background sink"))
        control = threading.Thread(target=run)
        background.start()
        try:
            sink_blocked = entered.wait(1)
            assert sink_blocked
            control.start()
            progressed = written.wait(0.25)
        finally:
            release.set()
            background.join(1)
            if control.ident is not None:
                control.join(1)
        assert progressed, "slow-stage logging blocked the control write"
        assert not background.is_alive() and not control.is_alive()
        assert not errors and results == [True]
        victron.set_grid_setpoint.assert_called_once_with(-100)
        pending = diagnostics.drain()
        assert pending == [
            {
                "event": "control_stage_slow",
                "stage": "get_system_data",
                "seconds": pytest.approx(0.4),
            }
        ]


@pytest.mark.parametrize("failure_phase", ["read", "console"])
def test_failed_iteration_records_duration_and_deadline_without_failed_write(failure_phase):
    now = [1000.0]

    def fail(*_args):
        now[0] += 0.4
        raise RuntimeError("fixture failure")

    with (
        control_fixture() as (controller, victron, _),
        patch.object(controller_module.time, "monotonic", side_effect=lambda: now[0]),
        patch.object(controller_module.time, "perf_counter", side_effect=lambda: now[0]),
        patch.object(controller_module, "log_exception") as error_log,
    ):
        if failure_phase == "read":
            victron.get_system_data.side_effect = fail
        else:
            controller.console.format_line.side_effect = fail
        result = controller.run_cycle()
    assert result is True
    error_log.assert_called_once()
    snapshot = controller.metrics.snapshot()
    assert snapshot["cycle_ms"]["samples"] == 1
    assert snapshot["cycle_ms"]["max"] == 400.0
    assert snapshot["cycle_ms"]["missed_deadlines"] == 1
    assert snapshot["setvalue_ms"]["failed"] == 0
    assert snapshot["setvalue_ms"]["samples"] == (0 if failure_phase == "read" else 1)
    if failure_phase == "read":
        victron.set_grid_setpoint.assert_not_called()
    else:
        victron.set_grid_setpoint.assert_called_once_with(-100)


def test_rejected_write_records_exactly_one_failed_write_and_cycle():
    with control_fixture() as (controller, victron, _):
        victron.set_grid_setpoint.return_value = False
        result = controller.run_cycle()
    assert result is True
    snapshot = controller.metrics.snapshot()
    assert snapshot["cycle_ms"]["samples"] == 1
    assert snapshot["setvalue_ms"]["samples"] == 1
    assert snapshot["setvalue_ms"]["failed"] == 1


def test_keyboard_interrupt_keeps_shutdown_outside_cycle_samples():
    with control_fixture() as (controller, victron, _):
        victron.get_system_data.side_effect = KeyboardInterrupt
        result = controller.run_cycle()
    assert result is False
    assert controller.metrics.snapshot()["cycle_ms"]["samples"] == 0
    victron.set_grid_setpoint.assert_not_called()


@pytest.mark.parametrize("buffer_state", ["full", "contended", "broken"])
def test_stage_diagnostic_loss_does_not_change_write_or_cycle(buffer_state):
    now = [1000.0]
    with (
        control_fixture() as (controller, victron, diagnostics),
        patch.object(controller_module.time, "monotonic", side_effect=lambda: now[0]),
        patch.object(controller_module.time, "perf_counter", side_effect=lambda: now[0]),
        patch.object(
            controller_module.logger, "warning", side_effect=AssertionError("control sink")
        ),
    ):

        def read_system():
            now[0] += 0.4
            return {"_grid_valid": True, "gt": 0}

        victron.get_system_data.side_effect = read_system
        if buffer_state == "full":
            for _ in range(CAPACITY):
                diagnostics.record("native_fallback")
        elif buffer_state == "contended":
            diagnostics._lock.acquire()
        try:
            if buffer_state == "broken":
                with patch.object(diagnostics, "record", side_effect=RuntimeError("buffer")):
                    result = controller.run_cycle()
            else:
                result = controller.run_cycle()
        finally:
            if buffer_state == "contended":
                diagnostics._lock.release()
    assert result is True
    victron.set_grid_setpoint.assert_called_once_with(-100)
    assert controller.metrics.snapshot()["cycle_ms"]["samples"] == 1
    pending = diagnostics.drain()
    if buffer_state == "full":
        assert len(pending) == CAPACITY
        assert pending[-1]["event"] == "control_stage_slow"
    else:
        assert pending == []


def test_background_drain_preserves_stage_warning_text_and_units():
    with (
        control_fixture() as (controller, _, diagnostics),
        patch.object(controller.metrics, "sample_process"),
        patch.object(controller_module, "prom_metrics_publish"),
        patch.object(controller_module.logger, "warning") as warning,
    ):
        diagnostics.record("control_stage_slow", stage="get_system_data", seconds=0.4)
        controller._read_performance()
    warning.assert_called_once_with("Control cycle stage %s slow: %.0fms", "get_system_data", 400.0)


def test_stage_diagnostic_fields_are_bounded_without_formatting_arbitrary_objects():
    class Unformattable:
        def __str__(self):
            raise AssertionError("arbitrary stage formatting")

    diagnostics = WriteDiagnostics()
    for stage, seconds in ((Unformattable(), 0.4), ("read", float("inf")), ("read", -1)):
        diagnostics.record("control_stage_slow", stage=stage, seconds=seconds)
    diagnostics.record("control_stage_slow", stage="s" * (MAX_TEXT + 100), seconds=0.4)
    records = diagnostics.drain()
    assert records == [{"event": "control_stage_slow", "seconds": 0.4, "stage": "s" * MAX_TEXT}]


def test_error_handler_failure_preserves_exception_and_still_records_cycle():
    now = [1000.0]

    def fail_read():
        now[0] += 0.4
        raise RuntimeError("read")

    def fail_logging(_message):
        now[0] += 0.05
        raise OSError("sink")

    with (
        control_fixture() as (controller, victron, _),
        patch.object(controller_module.time, "monotonic", side_effect=lambda: now[0]),
        patch.object(controller_module.time, "perf_counter", side_effect=lambda: now[0]),
        patch.object(controller_module, "log_exception", side_effect=fail_logging),
    ):
        victron.get_system_data.side_effect = fail_read
        with pytest.raises(OSError, match="sink"):
            controller.run_cycle()
    snapshot = controller.metrics.snapshot()
    assert snapshot["cycle_ms"]["samples"] == 1
    assert snapshot["cycle_ms"]["max"] == 450.0
    assert snapshot["cycle_ms"]["missed_deadlines"] == 1
    assert snapshot["setvalue_ms"]["failed"] == 0
    victron.set_grid_setpoint.assert_not_called()

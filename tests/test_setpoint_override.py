"""Daemon ownership outlives the desktop and serializes automatic writes."""

from unittest.mock import MagicMock, call

import pytest

from inverter_control.watchdog import HardwareWatchdog


@pytest.fixture
def manual(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inverter_control.watchdog.time.monotonic", lambda: now[0])
    victron = MagicMock()
    victron.set_grid_setpoint.return_value = True
    watchdog = HardwareWatchdog(victron, grid_loss_hold_seconds=20, dry_run=True)
    return watchdog, victron, now


def test_explicit_override_refreshes_every_two_seconds_in_dry_mode(manual):
    watchdog, victron, now = manual
    old_generation = watchdog.control_generation()
    assert watchdog.set_setpoint_override(-520, "start")["value"] == -520
    assert not watchdog.write_control_setpoint(10, old_generation)
    watchdog.mark_dbus_invalid()
    for now[0] in (100.1, 101.9, 102, 102.1, 103.9, 104):
        watchdog.check_grid_loss()
        watchdog._check_heartbeat()
    assert victron.set_grid_setpoint.call_args_list == [call(-520)] * 3
    watchdog.set_setpoint_override(None, "stop")
    now[0] = 108
    watchdog.check_grid_loss()
    assert victron.set_grid_setpoint.call_count == 3
    assert not watchdog.write_control_setpoint(10, old_generation, dry_run=True)
    assert watchdog.write_control_setpoint(10, watchdog.control_generation(), dry_run=True)
    assert HardwareWatchdog(victron).get_setpoint_override()["value"] is None


@pytest.mark.parametrize("value", [True, 1.5, "10", -(2**31) - 1, 2**31])
def test_rejected_edit_keeps_previously_accepted_override(manual, value):
    watchdog, victron, _ = manual
    watchdog.set_setpoint_override(-100)
    status = watchdog.set_setpoint_override(value, "bad edit")
    assert status["value"] == -100 and status["last_error"]
    assert status["request_id"] == "bad edit"
    victron.set_grid_setpoint.assert_called_once_with(-100)


def test_failed_write_keeps_override_and_failed_refresh_retries_on_cadence(manual):
    watchdog, victron, now = manual
    publish = MagicMock()
    watchdog.set_override_status_callback(publish)
    watchdog.set_setpoint_override(-100)
    victron.set_grid_setpoint.return_value = False
    assert watchdog.set_setpoint_override(-200)["value"] == -100
    now[0] = 102
    watchdog.check_grid_loss()
    status = watchdog.get_setpoint_override()
    assert status["last_error"] and status["value"] == -100
    attempts = victron.set_grid_setpoint.call_count
    now[0] = 103
    watchdog.check_grid_loss()
    assert victron.set_grid_setpoint.call_count == attempts
    victron.set_grid_setpoint.return_value = True
    watchdog.set_setpoint_override(-100)
    now[0] = 105
    watchdog.check_grid_loss()
    assert watchdog.get_setpoint_override()["last_error"] is None
    assert publish.call_args.args[0]["last_error"] is None

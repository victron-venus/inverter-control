"""Meter loss holds the last accepted command briefly, then latches zero."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock, call, patch

import pytest

from inverter_control.watchdog import HardwareWatchdog


@pytest.fixture(name="clock")
def _clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    return now


@pytest.fixture(name="watchdog_factory")
def _watchdog_factory(clock):
    def make(hold=3.0, *, accepted=True, dry_run=False):
        victron = MagicMock()
        victron.set_grid_setpoint.return_value = True
        watchdog = HardwareWatchdog(
            victron,
            timeout_seconds=30,
            check_interval=5,
            dry_run=dry_run,
            get_setpoint=lambda: -500,
            grid_loss_hold_seconds=hold,
        )
        watchdog.mark_dbus_update()
        if accepted:
            watchdog.mark_setpoint_update()
        return watchdog, victron

    return make


def test_hold_is_passive_and_expires_at_first_loss_deadline(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    clock[0] = 101.0
    watchdog.mark_dbus_invalid()
    watchdog.check_grid_loss()
    status = watchdog.get_status()
    assert status["grid_loss_state"] == "holding"
    assert status["grid_loss_hold_seconds"] == 3.0
    assert status["grid_loss_elapsed"] == pytest.approx(0.0)
    assert status["grid_loss_remaining"] == pytest.approx(3.0)
    victron.set_grid_setpoint.assert_not_called()

    for clock[0] in (102.0, 103.9):
        watchdog.mark_dbus_invalid()
        watchdog.check_grid_loss()
        watchdog._check_heartbeat()
        victron.set_grid_setpoint.assert_not_called()

    clock[0] = 104.0
    watchdog.mark_dbus_invalid()
    watchdog.check_grid_loss()
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert watchdog.is_triggered()
    assert watchdog.get_status()["grid_loss_state"] == "zero"
    assert watchdog.get_status()["grid_loss_remaining"] == 0
    assert watchdog.get_status()["dbus_age"] == 4.0
    assert watchdog.get_status()["setpoint_age"] == 4.0
    victron.set_ess_mode.assert_not_called()


def test_watchdog_thread_check_enforces_meter_deadline(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    watchdog.mark_dbus_invalid()
    clock[0] += 3.0
    watchdog._check_heartbeat()
    victron.set_grid_setpoint.assert_called_once_with(0)


def test_meter_loss_during_generic_failsafe_never_restores_old_command(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    clock[0] = 131.0
    for _ in range(3):
        watchdog._check_heartbeat()
    assert watchdog._pre_forced_setpoint == -500
    watchdog.mark_dbus_invalid()
    watchdog.check_grid_loss()
    clock[0] += 0.1
    watchdog.mark_dbus_update()
    for _ in range(2):
        watchdog._check_heartbeat()
    assert not watchdog.is_triggered()
    assert watchdog.get_status()["grid_loss_zero_applied"] is True
    victron.set_grid_setpoint.assert_called_once_with(0)


def test_control_and_watchdog_checks_share_a_single_zero_write(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    watchdog.mark_dbus_invalid()
    clock[0] += 3.0
    barrier = Barrier(3)

    def check(callback):
        barrier.wait(timeout=2)
        callback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        control_check = pool.submit(check, watchdog.check_grid_loss)
        background_check = pool.submit(check, watchdog._check_heartbeat)
        barrier.wait(timeout=2)
        control_check.result(timeout=2)
        background_check.result(timeout=2)
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert watchdog.get_status()["grid_loss_state"] == "zero"


@pytest.mark.parametrize("hold,accepted", [(0.0, True), (3.0, False)])
def test_zero_delay_or_unknown_startup_output_goes_directly_to_zero(
    watchdog_factory, hold, accepted
):
    watchdog, victron = watchdog_factory(hold, accepted=accepted)
    watchdog.mark_dbus_invalid()
    watchdog.check_grid_loss()
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert watchdog.is_triggered()


def test_recovery_before_deadline_cancels_hold_without_rewriting_command(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    watchdog.mark_dbus_invalid()
    watchdog.check_grid_loss()
    clock[0] += 2.9
    watchdog.mark_dbus_update()
    watchdog.check_grid_loss()
    watchdog._check_heartbeat()
    victron.set_grid_setpoint.assert_not_called()
    assert not watchdog.is_triggered()
    assert watchdog.get_status()["grid_loss_state"] == "normal"
    assert watchdog.get_status()["grid_loss_remaining"] is None

    # A later, distinct outage gets its own deadline.
    clock[0] = 110.0
    watchdog.mark_setpoint_update()
    watchdog.mark_dbus_invalid()
    clock[0] = 112.9
    watchdog.check_grid_loss()
    victron.set_grid_setpoint.assert_not_called()
    clock[0] = 113.0
    watchdog.check_grid_loss()
    victron.set_grid_setpoint.assert_called_once_with(0)


def test_long_loss_holds_zero_and_recovery_never_restores_pre_loss_command(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    watchdog.mark_dbus_invalid()
    clock[0] += 3.0
    watchdog.check_grid_loss()
    for clock[0] in (110.0, 150.0, 200.0):
        watchdog.mark_dbus_invalid()
        watchdog.check_grid_loss()
        watchdog._check_heartbeat()
    assert watchdog.is_triggered()
    assert all(item == call(0) for item in victron.set_grid_setpoint.call_args_list)

    watchdog.mark_dbus_update()
    watchdog._check_heartbeat()
    assert watchdog.is_triggered()
    assert watchdog.get_status()["grid_loss_state"] == "recovering"
    watchdog._check_heartbeat()
    assert not watchdog.is_triggered()
    assert watchdog.get_status()["grid_loss_state"] == "normal"
    assert all(item == call(0) for item in victron.set_grid_setpoint.call_args_list)
    victron.set_ess_mode.assert_not_called()


def test_recovery_observed_after_deadline_cannot_skip_required_zero(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    watchdog.mark_dbus_invalid()
    watchdog.check_grid_loss()
    clock[0] += 4.0
    # The main loop was delayed between checks; the first new read is valid.
    watchdog.mark_dbus_update()
    watchdog.check_grid_loss()
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert watchdog.is_triggered()
    watchdog._check_heartbeat()
    assert watchdog.is_triggered()
    watchdog._check_heartbeat()
    assert not watchdog.is_triggered()
    victron.set_grid_setpoint.assert_called_once_with(0)


@pytest.mark.parametrize("failure", [False, RuntimeError("D-Bus unavailable")])
def test_failed_zero_is_retried_after_valid_data_returns(clock, watchdog_factory, failure):
    watchdog, victron = watchdog_factory()
    victron.set_grid_setpoint.side_effect = [failure, True]
    watchdog.mark_dbus_invalid()
    clock[0] += 3.0
    watchdog.check_grid_loss()
    assert watchdog.is_triggered()
    assert watchdog.get_status()["grid_loss_state"] == "zero_pending"

    clock[0] += 0.1
    watchdog.mark_dbus_update()
    watchdog.check_grid_loss()
    for _ in range(2):
        watchdog._check_heartbeat()
    assert victron.set_grid_setpoint.call_args_list == [call(0), call(0)]
    assert not watchdog.is_triggered()


def test_brief_valid_sample_does_not_rearm_zero_latch(clock, watchdog_factory):
    watchdog, victron = watchdog_factory()
    watchdog.mark_dbus_invalid()
    clock[0] += 3.0
    watchdog.check_grid_loss()
    watchdog.mark_dbus_update()
    watchdog._check_heartbeat()
    assert watchdog.is_triggered()

    watchdog.mark_dbus_invalid()
    for _ in range(3):
        watchdog.check_grid_loss()
        watchdog._check_heartbeat()
    assert watchdog.is_triggered()
    assert all(item == call(0) for item in victron.set_grid_setpoint.call_args_list)

    watchdog.mark_dbus_update()
    watchdog._check_heartbeat()
    assert watchdog.is_triggered()
    watchdog._check_heartbeat()
    assert not watchdog.is_triggered()
    assert all(item == call(0) for item in victron.set_grid_setpoint.call_args_list)


@pytest.mark.parametrize("wall_time", [-10000.0, 10000000000.0])
def test_wall_clock_jump_does_not_change_hold_deadline(
    clock, watchdog_factory, monkeypatch, wall_time
):
    watchdog, victron = watchdog_factory()
    watchdog.mark_dbus_invalid()
    monkeypatch.setattr("time.time", lambda: wall_time)
    clock[0] += 2.9
    watchdog.check_grid_loss()
    victron.set_grid_setpoint.assert_not_called()
    clock[0] += 0.1
    watchdog.check_grid_loss()
    victron.set_grid_setpoint.assert_called_once_with(0)


def test_disabled_option_preserves_legacy_timeout_and_restore(clock, watchdog_factory):
    watchdog, victron = watchdog_factory(None)
    watchdog.mark_dbus_invalid()
    clock[0] += 3.0
    watchdog.check_grid_loss()
    watchdog._check_heartbeat()
    victron.set_grid_setpoint.assert_not_called()
    assert watchdog.get_status()["grid_loss_state"] == "disabled"

    clock[0] = 131.0
    for _ in range(3):
        watchdog._check_heartbeat()
    victron.set_grid_setpoint.assert_called_once_with(0)
    watchdog.mark_dbus_update()
    for _ in range(2):
        watchdog._check_heartbeat()
    assert victron.set_grid_setpoint.call_args_list == [call(0), call(-500)]


@pytest.mark.parametrize("accepted", [False, True])
def test_dry_run_never_writes_during_loss_or_recovery(clock, watchdog_factory, accepted):
    watchdog, victron = watchdog_factory(accepted=accepted, dry_run=True)
    watchdog.mark_dbus_invalid()
    for clock[0] in (100.0, 103.0, 150.0):
        watchdog.check_grid_loss()
        watchdog._check_heartbeat()
    watchdog.mark_dbus_update()
    for _ in range(3):
        watchdog._check_heartbeat()
    victron.set_grid_setpoint.assert_not_called()
    victron.set_ess_mode.assert_not_called()


@pytest.fixture(name="control")
def _control(clock):
    from test_main import _make_controller

    with patch("inverter_control.controller.get_webhook_server"):
        controller, victron, _, _ = _make_controller()
    controller._update_dvcc_limits = MagicMock()
    controller.calculate_setpoint = MagicMock(return_value=(-400, ""))
    controller.handle_minimize_charging = MagicMock()
    controller.update_state = MagicMock()
    controller.get_boolean = MagicMock(return_value=False)
    controller.previous_setpoint = -500
    controller._watchdog = HardwareWatchdog(
        victron,
        grid_loss_hold_seconds=3.0,
        get_setpoint=lambda: controller.previous_setpoint,
    )
    controller._watchdog.mark_dbus_update()
    controller._watchdog.mark_setpoint_update()
    victron.get_system_data.return_value = {"_grid_valid": True, "gt": 100}
    victron.get_grid_status.return_value = {"_grid_valid": True}
    victron.set_grid_setpoint.return_value = True
    with patch("inverter_control.controller.broadcast_line"):
        yield controller, victron


def test_controller_loss_gate_freezes_control_and_preserves_manual_request(clock, control):
    controller, victron = control
    controller.manual_setpoint = -700
    victron.get_system_data.return_value = {
        "_grid_valid": False,
        "_grid_invalid_reason": "Grid measurement source changed",
        "gt": -600,
    }
    for clock[0] in (100.0, 101.0, 102.9):
        assert controller.run_cycle()
        victron.set_grid_setpoint.assert_not_called()
    controller.calculate_setpoint.assert_not_called()
    controller.handle_minimize_charging.assert_not_called()
    assert controller.manual_setpoint == -700
    assert controller.previous_setpoint == -500

    clock[0] = 103.0
    assert controller.run_cycle()
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert controller.manual_setpoint == -700
    assert not controller.state["grid_control_valid"]
    victron.set_ess_mode.assert_not_called()


def test_controller_prewrite_loss_check_rejects_calculated_command(clock, control):
    controller, victron = control

    def lose_meter(_snapshot):
        victron.get_grid_status.return_value = {
            "_grid_valid": False,
            "_grid_invalid_reason": "Grid source owner changed",
        }
        return -900, ""

    controller.calculate_setpoint.side_effect = lose_meter
    assert controller.run_cycle()
    victron.set_grid_setpoint.assert_not_called()
    assert controller.previous_setpoint == -500
    assert controller._watchdog.get_status()["grid_loss_state"] == "holding"

    victron.get_system_data.return_value = {"_grid_valid": False}
    clock[0] += 3.0
    assert controller.run_cycle()
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert controller.calculate_setpoint.call_count == 1


def test_controller_resumes_from_accepted_zero_after_background_recovery(clock, control):
    controller, victron = control
    controller._watchdog.mark_dbus_invalid()
    clock[0] += 3.0
    controller._watchdog._check_heartbeat()
    assert controller.previous_setpoint == -500

    controller._watchdog.mark_dbus_update()
    controller._watchdog._check_heartbeat()
    controller._watchdog._check_heartbeat()
    assert not controller._watchdog.is_triggered()

    def calculate_from_applied_zero(_snapshot):
        assert controller.previous_setpoint == 0
        return -100, ""

    controller.calculate_setpoint.side_effect = calculate_from_applied_zero
    assert controller.run_cycle()
    assert victron.set_grid_setpoint.call_args_list == [call(0), call(-100)]
    assert controller.previous_setpoint == -100
    assert not controller._watchdog.get_status()["grid_loss_zero_applied"]


def test_controller_rejected_zero_does_not_claim_output_changed(clock, control):
    controller, victron = control
    victron.get_system_data.return_value = {"_grid_valid": False}
    victron.set_grid_setpoint.side_effect = [False, True]
    assert controller.run_cycle()
    clock[0] += 3.0
    assert controller.run_cycle()
    assert controller.previous_setpoint == -500
    assert controller.state["grid_loss_state"] == "zero_pending"
    assert not controller.state["grid_loss_zero_applied"]

    assert controller.run_cycle()
    assert controller.previous_setpoint == 0
    assert controller.state["setpoint"] == 0
    assert controller.state["grid_loss_zero_applied"]

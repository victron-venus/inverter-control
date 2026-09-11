"""Regression tests for failed safety writes and wall-clock adjustments."""

from unittest.mock import Mock, call, patch

import pytest

from inverter_control.watchdog import HardwareWatchdog


def test_failed_recovery_snapshot_does_not_block_failsafe():
    victron = Mock()
    victron.set_grid_setpoint.return_value = True
    watchdog = HardwareWatchdog(victron, get_setpoint=Mock(side_effect=RuntimeError("unavailable")))
    with patch("inverter_control.watchdog.time.monotonic", return_value=100):
        for _ in range(watchdog._fail_threshold):
            watchdog._check_heartbeat()
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert watchdog.get_status()["hardware_forced"]
    assert watchdog._pre_forced_setpoint == 0


@pytest.mark.parametrize("failure", [False, RuntimeError("D-Bus unavailable")])
def test_failed_failsafe_retries_until_accepted(failure):
    victron = Mock()
    victron.set_grid_setpoint.side_effect = [failure, failure, True]
    watchdog = HardwareWatchdog(victron, get_setpoint=lambda: 1234)
    with patch("inverter_control.watchdog.time.monotonic", return_value=100):
        for _ in range(watchdog._fail_threshold):
            watchdog._check_heartbeat()
        assert watchdog.is_triggered()
        assert not watchdog.get_status()["hardware_forced"]
        watchdog._check_heartbeat()
        assert not watchdog.get_status()["hardware_forced"]
        watchdog._check_heartbeat()
        assert watchdog.get_status()["hardware_forced"]
        for _ in range(5):
            watchdog._check_heartbeat()
    assert victron.set_grid_setpoint.call_args_list == [call(0)] * 3
    victron.set_ess_mode.assert_not_called()


@pytest.mark.parametrize("failure", [False, RuntimeError("D-Bus unavailable")])
def test_failed_restore_remains_armed_and_retries(failure):
    victron = Mock()
    victron.set_grid_setpoint.side_effect = [True, failure, True]
    watchdog = HardwareWatchdog(victron, get_setpoint=lambda: 1234)
    with patch("inverter_control.watchdog.time.monotonic", return_value=100):
        for _ in range(watchdog._fail_threshold):
            watchdog._check_heartbeat()
        watchdog.mark_dbus_update()
        watchdog.mark_setpoint_update()
        for _ in range(watchdog._success_threshold):
            watchdog._check_heartbeat()
        assert watchdog.is_triggered()
        assert watchdog.get_status()["hardware_forced"]
        watchdog._check_heartbeat()
    assert not watchdog.is_triggered()
    assert victron.set_grid_setpoint.call_args_list == [call(0), call(1234), call(1234)]


@pytest.mark.parametrize("wall_time", [-10000, 10000000000])
def test_wall_clock_changes_do_not_delay_or_accelerate_timeout(wall_time):
    victron = Mock()
    victron.set_grid_setpoint.return_value = True
    watchdog = HardwareWatchdog(victron, timeout_seconds=30)
    with patch("inverter_control.watchdog.time.monotonic", return_value=100):
        watchdog.mark_dbus_update()
        watchdog.mark_setpoint_update()
        watchdog.mark_mqtt_update()
    with (
        patch("inverter_control.watchdog.time.time", return_value=wall_time),
        patch("inverter_control.watchdog.time.monotonic", return_value=110),
    ):
        for _ in range(5):
            watchdog._check_heartbeat()
        assert not watchdog.is_triggered()
        assert watchdog.get_status()["setpoint_age"] == 10
    with patch("inverter_control.watchdog.time.monotonic", return_value=131):
        for _ in range(watchdog._fail_threshold):
            watchdog._check_heartbeat()
        assert watchdog.get_status()["dbus_age"] == 31
    victron.set_grid_setpoint.assert_called_once_with(0)

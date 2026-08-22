"""Tests for TOU expensive-window suppression of forecast pre-charge."""

import unittest
from unittest.mock import MagicMock, patch

import inverter_control.config as _config
from inverter_control.controller import InverterController
from inverter_control.victron import TOU_END_SETTING, TOU_START_SETTING


def _make_controller() -> InverterController:
    # Skip __init__ (heavy D-Bus wiring); tests mock victron methods.
    ctrl = InverterController.__new__(InverterController)
    ctrl.victron = MagicMock()
    ctrl._tou_cache = None
    ctrl._tou_cache_time = 0.0
    return ctrl


def _set_dbus_hours(ctrl: InverterController, start: int, end: int):
    """Make the mocked victron return the given hours from localsettings."""
    ctrl.victron.get_tou_setting.side_effect = lambda path: {
        TOU_START_SETTING: start,
        TOU_END_SETTING: end,
    }[path]


class TestExpensiveWindow(unittest.TestCase):
    def test_disabled_when_hours_negative(self):
        ctrl = _make_controller()
        _set_dbus_hours(ctrl, -1, -1)
        with patch.object(ctrl.victron, "get_local_hour", return_value=16):
            self.assertFalse(ctrl._in_expensive_window())

    def test_disabled_when_equal(self):
        ctrl = _make_controller()
        _set_dbus_hours(ctrl, 8, 8)
        with patch.object(ctrl.victron, "get_local_hour", return_value=9):
            self.assertFalse(ctrl._in_expensive_window())

    def test_inside_plain_window(self):
        ctrl = _make_controller()
        _set_dbus_hours(ctrl, 15, 24)
        with patch.object(ctrl.victron, "get_local_hour", return_value=16):
            self.assertTrue(ctrl._in_expensive_window())

    def test_outside_plain_window_edges(self):
        ctrl = _make_controller()
        _set_dbus_hours(ctrl, 15, 24)
        with patch.object(ctrl.victron, "get_local_hour", return_value=14) as hour:
            self.assertFalse(ctrl._in_expensive_window())
            hour.return_value = 0
            self.assertFalse(ctrl._in_expensive_window())
            # end hour is exclusive: 24 wraps to 0
            hour.return_value = 23
            self.assertTrue(ctrl._in_expensive_window())

    def test_falls_back_to_config_when_setting_unreadable(self):
        ctrl = _make_controller()
        ctrl.victron.get_tou_setting.return_value = None
        with (
            patch.object(_config, "TOU_EXPENSIVE_START_HOUR", 20),
            patch.object(_config, "TOU_EXPENSIVE_END_HOUR", 23),
            patch.object(ctrl.victron, "get_local_hour", return_value=21),
        ):
            self.assertTrue(ctrl._in_expensive_window())

    def test_values_cached_within_ttl(self):
        ctrl = _make_controller()
        _set_dbus_hours(ctrl, 15, 24)
        with patch.object(ctrl.victron, "get_local_hour", return_value=16):
            ctrl._in_expensive_window()
            _set_dbus_hours(ctrl, -1, -1)  # would disable, but cache holds 15/24
            self.assertTrue(ctrl._in_expensive_window())


if __name__ == "__main__":
    unittest.main()

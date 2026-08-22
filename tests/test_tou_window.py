"""Tests for TOU expensive-window suppression of forecast pre-charge."""

import unittest
from unittest.mock import patch

import inverter_control.config as _config
from inverter_control.controller import InverterController


def _make_controller() -> InverterController:
    # Skip __init__ (heavy D-Bus wiring); _in_expensive_window uses no
    # instance state, only config + local time.
    return InverterController.__new__(InverterController)


class TestExpensiveWindow(unittest.TestCase):
    def test_disabled_when_hours_negative(self):
        with (
            patch.object(_config, "TOU_EXPENSIVE_START_HOUR", -1),
            patch.object(_config, "TOU_EXPENSIVE_END_HOUR", -1),
        ):
            self.assertFalse(_make_controller()._in_expensive_window())

    def test_disabled_when_equal(self):
        with (
            patch.object(_config, "TOU_EXPENSIVE_START_HOUR", 8),
            patch.object(_config, "TOU_EXPENSIVE_END_HOUR", 8),
        ):
            self.assertFalse(_make_controller()._in_expensive_window())

    def test_inside_plain_window(self):
        with (
            patch.object(_config, "TOU_EXPENSIVE_START_HOUR", 15),
            patch.object(_config, "TOU_EXPENSIVE_END_HOUR", 24),
            patch("time.localtime") as lt,
        ):
            lt.return_value.tm_hour = 16
            self.assertTrue(_make_controller()._in_expensive_window())

    def test_outside_plain_window_edges(self):
        ctrl = _make_controller()
        with (
            patch.object(_config, "TOU_EXPENSIVE_START_HOUR", 15),
            patch.object(_config, "TOU_EXPENSIVE_END_HOUR", 24),
            patch("time.localtime") as lt,
        ):
            lt.return_value.tm_hour = 14
            self.assertFalse(ctrl._in_expensive_window())
            lt.return_value.tm_hour = 0
            self.assertFalse(ctrl._in_expensive_window())
            # end hour is exclusive: 24 wraps to 0
            lt.return_value.tm_hour = 23
            self.assertTrue(ctrl._in_expensive_window())


if __name__ == "__main__":
    unittest.main()

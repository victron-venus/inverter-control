"""Tests for GUI-editable TOU localsettings creation (AddSetting path)."""

import unittest
from unittest.mock import patch

from inverter_control.victron import (
    SETTINGS_SERVICE,
    TOU_START_SETTING,
    VictronDBus,
)


def _make_victron() -> VictronDBus:
    v = VictronDBus.__new__(VictronDBus)
    v._dbus_lock = __import__("threading").Lock()
    v._consecutive_errors = 0
    v._last_success_time = 0.0
    return v


def _dbus_get_side_effect(existing: dict):
    return lambda service, path: existing.get(path)


class TestDbusAddSetting(unittest.TestCase):
    def test_success_returns_true(self):
        v = _make_victron()
        with patch.object(v, "_safe_subprocess", return_value="method return\n   int32 0"):
            self.assertTrue(v._dbus_add_setting("InverterControl", "TouExpensiveStartHour", 15))

    def test_error_code_returns_false(self):
        v = _make_victron()
        with patch.object(v, "_safe_subprocess", return_value="method return\n   int32 -3"):
            self.assertFalse(v._dbus_add_setting("InverterControl", "Bad", 5))

    def test_garbage_reply_returns_false(self):
        v = _make_victron()
        with patch.object(v, "_safe_subprocess", return_value="not-a-number"):
            self.assertFalse(v._dbus_add_setting("InverterControl", "Bad", 5))

    def test_no_reply_returns_false(self):
        v = _make_victron()
        with patch.object(v, "_safe_subprocess", return_value=None):
            self.assertFalse(v._dbus_add_setting("InverterControl", "Bad", 5))


class TestEnsureTouSettings(unittest.TestCase):
    def test_creates_missing_settings(self):
        v = _make_victron()
        with (
            patch.object(
                v,
                "_dbus_get",
                side_effect=_dbus_get_side_effect({}),
            ) as get,
            patch.object(v, "_dbus_add_setting", return_value=True) as add,
        ):
            v.ensure_tou_settings(15, 24)
        self.assertEqual(get.call_count, 2)
        calls = [c.args for c in add.call_args_list]
        self.assertIn(("InverterControl", "TouExpensiveStartHour", 15), calls)
        self.assertIn(("InverterControl", "TouExpensiveEndHour", 24), calls)

    def test_existing_settings_not_overwritten(self):
        v = _make_victron()
        with (
            patch.object(
                v,
                "_dbus_get",
                side_effect=_dbus_get_side_effect({TOU_START_SETTING: "20"}),
            ),
            patch.object(v, "_dbus_add_setting", return_value=True) as add,
        ):
            v.ensure_tou_settings(15, 24)
        # Only EndHour created; StartHour kept at its existing value 20
        self.assertEqual(add.call_count, 1)
        self.assertEqual(add.call_args.args[:2], ("InverterControl", "TouExpensiveEndHour"))

    def test_service_name_used(self):
        v = _make_victron()
        with patch.object(v, "_dbus_get", side_effect=_dbus_get_side_effect({})) as get:
            v.get_tou_setting(TOU_START_SETTING)
            self.assertEqual(get.call_args.args[0], SETTINGS_SERVICE)


if __name__ == "__main__":
    unittest.main()

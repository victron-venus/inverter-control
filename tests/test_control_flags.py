"""Control flags: in-process getter, MQTT toggle, Settings persist."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_main import _make_controller
from test_tou_settings import _dbus_get_side_effect, _make_victron

import main
from inverter_control.logic import SystemState
from inverter_control.victron import CONTROL_FLAG_KEYS, CONTROL_FLAG_SETTINGS, SETTINGS_SERVICE

_MOD = "inverter_control.controller"


class TestControlFlagGetter(unittest.TestCase):
    def test_set_boolean_is_seen_by_calculate_setpoint_not_ha(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        mock_victron.get_mppt_data.return_value = {}
        mock_victron.get_pv_power.return_value = []
        mock_victron.get_inverter_power.return_value = 0
        mock_ha.get_boolean.return_value = False  # HA still off
        mock_ha.get_vue_sensor.return_value = 0
        mock_calc.calculate.return_value = MagicMock(setpoint=0, flags="", filtered_gt=0.0)

        controller.set_boolean("only_charging", True)
        controller.calculate_setpoint(
            {
                "g1": 0,
                "g2": 0,
                "gt": 0,
                "t1": 0,
                "t2": 0,
                "tt": 0,
                "bv": 0,
                "bc": 0,
                "bp": 0,
                "soc": 0,
            }
        )

        state_arg = mock_calc.calculate.call_args[0][0]
        assert isinstance(state_arg, SystemState)
        assert state_arg.only_charging is True
        mock_ha.get_boolean.assert_not_called()

    def test_get_boolean_reads_internal_dict(self):
        controller, _, mock_ha, _ = _make_controller()
        mock_ha.get_boolean.return_value = True
        assert controller.get_boolean("no_feed") is False
        controller.set_boolean("no_feed", True)
        assert controller.get_boolean("no_feed") is True
        mock_ha.get_boolean.assert_not_called()


class TestMqttToggle(unittest.TestCase):
    def test_input_boolean_updates_internal_flag(self):
        controller, _, mock_ha, _ = _make_controller()
        main._handle_toggle(controller, {"entity": "input_boolean.only_charging", "state": "on"})
        assert controller.get_boolean("only_charging") is True
        mock_ha.toggle_entity.assert_not_called()

    def test_bare_key_and_payload_aliases(self):
        controller, _, _, _ = _make_controller()
        main._handle_toggle(controller, {"entity": "house_support", "state": "true"})
        assert controller.get_boolean("house_support") is True
        main._handle_toggle(controller, {"entity": "house_support", "state": "0"})
        assert controller.get_boolean("house_support") is False
        main._handle_toggle(controller, {"entity": "house_support", "state": "True"})
        assert controller.get_boolean("house_support") is True
        main._handle_toggle(controller, {"entity": "house_support", "state": False})
        assert controller.get_boolean("house_support") is False
        main._handle_toggle(controller, {"entity": "house_support", "state": 1})
        assert controller.get_boolean("house_support") is True

    def test_missing_state_toggles_current_value(self):
        controller, _, mock_ha, _ = _make_controller()
        controller.set_boolean("no_feed", True)
        main._handle_toggle(controller, {"entity": "input_boolean.no_feed"})
        assert controller.get_boolean("no_feed") is False
        mock_ha.toggle_entity.assert_not_called()

    def test_missing_state_does_not_force_off_when_already_off(self):
        controller, _, _, _ = _make_controller()
        assert controller.get_boolean("charge_battery") is False
        main._handle_toggle(controller, {"entity": "charge_battery"})
        assert controller.get_boolean("charge_battery") is True

    def test_non_boolean_toggle_reaches_ha(self):
        controller, _, mock_ha, _ = _make_controller()
        main._handle_toggle(controller, {"entity": "switch.recliner_recliner"})
        mock_ha.toggle_entity.assert_called_once_with("switch.recliner_recliner")
        main._handle_toggle(controller, {"entity": "switch.garage_opener_l"})
        main._handle_toggle(controller, {"entity": "switch.laundry_zigbee_switch"})
        main._handle_toggle(controller, {"entity": "switch.your_dump_load_1"})
        assert mock_ha.toggle_entity.call_count == 4

    def test_parse_mqtt_bool(self):
        assert main._parse_mqtt_bool("on") is True
        assert main._parse_mqtt_bool("OFF") is False
        assert main._parse_mqtt_bool("True") is True
        assert main._parse_mqtt_bool("false") is False
        assert main._parse_mqtt_bool("1") is True
        assert main._parse_mqtt_bool("0") is False
        assert main._parse_mqtt_bool(True) is True
        assert main._parse_mqtt_bool(0) is False
        assert main._parse_mqtt_bool("maybe") is None


class TestSettingsPersist(unittest.TestCase):
    def test_set_boolean_writes_settings(self):
        controller, mock_victron, _, _ = _make_controller()
        mock_victron._test_mode = False
        mock_victron.set_control_flag.return_value = True
        controller.set_boolean("only_charging", True)
        mock_victron.set_control_flag.assert_called_once_with("only_charging", 1)
        controller.set_boolean("only_charging", False)
        mock_victron.set_control_flag.assert_called_with("only_charging", 0)

    def test_set_boolean_survives_settings_write_failure(self):
        controller, mock_victron, _, _ = _make_controller()
        mock_victron._test_mode = False
        mock_victron.set_control_flag.side_effect = RuntimeError("dbus down")
        controller.set_boolean("no_feed", True)
        assert controller.get_boolean("no_feed") is True

    def test_startup_all_false_ignores_settings_and_ha(self):
        controller, mock_victron, mock_ha, _ = _make_controller()
        mock_victron._test_mode = False
        mock_victron.get_control_flag.side_effect = lambda k: 1  # last run ON
        mock_ha.fetch_boolean.return_value = True
        mock_ha.get_boolean.return_value = True
        mock_victron.set_control_flag.return_value = True
        with patch(f"{_MOD}.ENABLE_HA", True):
            controller._load_control_flags()
        for key in CONTROL_FLAG_KEYS:
            assert controller.get_boolean(key) is False
        mock_ha.fetch_boolean.assert_not_called()
        mock_ha.get_boolean.assert_not_called()
        mock_victron.get_control_flag.assert_not_called()
        seeds = mock_victron.ensure_control_flag_settings.call_args[0][0]
        assert seeds == {k: 0 for k in CONTROL_FLAG_KEYS}
        assert mock_victron.set_control_flag.call_count == len(CONTROL_FLAG_KEYS)
        for key in CONTROL_FLAG_KEYS:
            mock_victron.set_control_flag.assert_any_call(key, 0)

    def test_mqtt_cmd_flips_flag_without_ha_rest(self):
        controller, _, mock_ha, _ = _make_controller()
        assert controller.get_boolean("only_charging") is False
        main._handle_toggle(controller, {"entity": "input_boolean.only_charging", "state": "on"})
        assert controller.get_boolean("only_charging") is True
        mock_ha.fetch_boolean.assert_not_called()
        mock_ha.get_boolean.assert_not_called()
        mock_ha.toggle_entity.assert_not_called()


class TestControlFlagSettingsVictron(unittest.TestCase):
    def test_paths_are_pascal_case_under_invertercontrol(self):
        assert CONTROL_FLAG_SETTINGS["only_charging"] == ("/Settings/InverterControl/OnlyCharging")
        assert CONTROL_FLAG_SETTINGS["do_not_supply_charger"] == (
            "/Settings/InverterControl/DoNotSupplyCharger"
        )
        assert set(CONTROL_FLAG_SETTINGS) == set(CONTROL_FLAG_KEYS)

    def test_add_setting_uses_0_1_range_for_flags(self):
        v = _make_victron()
        with patch.object(v, "_safe_subprocess", return_value="method return\n   int32 0") as sub:
            self.assertTrue(v._dbus_add_setting("InverterControl", "OnlyCharging", 1, 0, 1))
        cmd = sub.call_args[0][0]
        self.assertIn("variant:int32:0", cmd)
        self.assertIn("variant:int32:1", cmd)
        self.assertIn("variant:int32:1", cmd)  # value and max

    def test_ensure_creates_missing_flags(self):
        v = _make_victron()
        with (
            patch.object(v, "_dbus_get", side_effect=_dbus_get_side_effect({})),
            patch.object(v, "_dbus_add_setting", return_value=True) as add,
        ):
            created = v.ensure_control_flag_settings({"only_charging": 1})
        self.assertEqual(set(created), set(CONTROL_FLAG_KEYS))
        first = add.call_args_list[0]
        self.assertEqual(first.kwargs.get("min_val", first[1].get("min_val")), 0)
        self.assertEqual(first.kwargs.get("max_val", first[1].get("max_val")), 1)
        names = [c.args[1] for c in add.call_args_list]
        self.assertIn("OnlyCharging", names)

    def test_ensure_does_not_overwrite_existing(self):
        v = _make_victron()
        existing = {CONTROL_FLAG_SETTINGS["only_charging"]: "1"}
        with (
            patch.object(v, "_dbus_get", side_effect=_dbus_get_side_effect(existing)),
            patch.object(v, "_dbus_add_setting", return_value=True) as add,
        ):
            created = v.ensure_control_flag_settings()
        self.assertNotIn("only_charging", created)
        self.assertEqual(add.call_count, len(CONTROL_FLAG_KEYS) - 1)

    def test_set_control_flag_writes_int32(self):
        v = _make_victron()
        path = CONTROL_FLAG_SETTINGS["no_feed"]
        with (
            patch.object(v, "_dbus_get", side_effect=_dbus_get_side_effect({path: "0"})),
            patch.object(v, "_dbus_set", return_value=True) as setter,
        ):
            self.assertTrue(v.set_control_flag("no_feed", 1))
        setter.assert_called_once_with(SETTINGS_SERVICE, path, 1, "int32")

    def test_get_control_flag_uses_settings_service(self):
        v = _make_victron()
        with patch.object(v, "_dbus_get", return_value="1") as get:
            self.assertEqual(v.get_control_flag("only_charging"), 1)
        self.assertEqual(get.call_args.args[0], SETTINGS_SERVICE)
        self.assertEqual(get.call_args.args[1], CONTROL_FLAG_SETTINGS["only_charging"])

    def test_unknown_key_is_noop(self):
        v = _make_victron()
        self.assertIsNone(v.get_control_flag("not_a_flag"))
        self.assertFalse(v.set_control_flag("not_a_flag", 1))

    def test_set_control_flag_creates_when_missing(self):
        v = _make_victron()
        with (
            patch.object(v, "_dbus_get", return_value=None),
            patch.object(v, "_dbus_add_setting", return_value=True) as add,
            patch.object(v, "_dbus_set") as setter,
        ):
            self.assertTrue(v.set_control_flag("only_charging", True))
        add.assert_called_once()
        self.assertEqual(add.call_args.args[:3], ("InverterControl", "OnlyCharging", 1))
        setter.assert_not_called()


class TestLoadFlagsNoHa(unittest.TestCase):
    def test_startup_registers_zeros_without_ha(self):
        controller, mock_victron, mock_ha, _ = _make_controller()
        mock_victron._test_mode = False
        mock_victron.set_control_flag.return_value = True
        with patch(f"{_MOD}.ENABLE_HA", False):
            controller._load_control_flags()
        mock_victron.ensure_control_flag_settings.assert_called_once()
        seeds = mock_victron.ensure_control_flag_settings.call_args[0][0]
        assert seeds == {k: 0 for k in CONTROL_FLAG_KEYS}
        for key in CONTROL_FLAG_KEYS:
            assert controller.get_boolean(key) is False
        mock_ha.fetch_boolean.assert_not_called()
        mock_ha.get_boolean.assert_not_called()


if __name__ == "__main__":
    unittest.main()

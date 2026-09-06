"""
Unit tests for InverterController in controller.py
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main
from inverter_control.logic import SystemState

_MOD = "inverter_control.controller"


def _make_controller(**overrides):
    """Build an InverterController with all subsystems mocked."""
    with (
        patch(f"{_MOD}.get_victron") as mock_get_victron,
        patch(f"{_MOD}.get_ha") as mock_get_ha,
        patch(f"{_MOD}.ConsoleUI") as mock_console_cls,
        patch(f"{_MOD}.SetpointCalculator") as mock_calc_cls,
        patch(f"{_MOD}.EvChargerReader") as mock_evcharger_cls,
        patch("inverter_control.config.get_ui_config", return_value={}),
        patch(f"{_MOD}.DRY_RUN", False),
        patch(f"{_MOD}.LOOP_INTERVAL", 0.33),
        patch(f"{_MOD}.POWER_LIMIT_MIN", -2300),
        patch(f"{_MOD}.POWER_LIMIT_MAX", 2250),
        patch(f"{_MOD}.DVCC_ENABLED", False),
        patch(f"{_MOD}.ENABLE_GRID_SMOOTHING_WITH_HOME", False),
        patch(f"{_MOD}.ENABLE_EV", True),
        patch(f"{_MOD}.ENABLE_WATER", True),
        patch(f"{_MOD}.ENABLE_HA", True),
    ):
        mock_victron = MagicMock()
        mock_ha = MagicMock()
        mock_console = MagicMock()
        mock_calc = MagicMock()
        mock_evcharger = MagicMock()
        mock_evcharger.read.return_value = {
            "ev_power": 0.0,
            "car_soc": 0,
            "ev_charging_kw": 0.0,
        }

        mock_get_victron.return_value = mock_victron
        mock_get_ha.return_value = mock_ha
        mock_console_cls.return_value = mock_console
        mock_calc_cls.return_value = mock_calc
        mock_evcharger_cls.return_value = mock_evcharger

        mock_victron.get_cell_counts.return_value = {}
        mock_calc.power_limit_min = -2300
        mock_calc.power_limit_max = 2250

        from inverter_control.controller import InverterController

        controller = InverterController(dry_run=overrides.get("dry_run", False))
        controller.victron = mock_victron
        controller.ha = mock_ha
        controller.console = mock_console
        controller.calculator = mock_calc
        controller.evcharger = mock_evcharger
        return controller, mock_victron, mock_ha, mock_calc


class TestInverterControllerInit(unittest.TestCase):
    """Test InverterController.__init__"""

    def test_init_sets_default_state(self):
        controller, mock_victron, mock_ha, _ = _make_controller()
        assert controller.current_setpoint == 0
        assert controller.previous_setpoint == 0
        assert controller.manual_setpoint is None
        assert controller.delay == 0
        assert controller.filtered_gt is None
        assert controller.loop_count == 0
        assert controller.state == {}
        assert controller.victron is mock_victron
        assert controller.ha is mock_ha
        assert controller.dry_run is False

    def test_init_creates_watchdog(self):
        controller, _, _, _ = _make_controller()
        from inverter_control.watchdog import HardwareWatchdog

        assert isinstance(controller._watchdog, HardwareWatchdog)

    def test_init_dry_run(self):
        controller, _, _, _ = _make_controller(dry_run=True)
        assert controller.dry_run is True


class TestCalculateSetpoint(unittest.TestCase):
    """Test InverterController.calculate_setpoint()"""

    def test_returns_tuple_of_int_and_str(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        mock_victron.get_mppt_data.return_value = {"mppt0": {"w": 100.0, "a": 2.0}}
        mock_victron.get_pv_power.return_value = [50.0]
        mock_victron.get_inverter_power.return_value = -500
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False
        mock_calc.calculate.return_value = MagicMock(setpoint=-600, flags="[T]", filtered_gt=100.0)

        sys_data = {
            "g1": 200,
            "g2": 0,
            "gt": 200,
            "t1": 300,
            "t2": 0,
            "tt": 300,
            "bv": 52.0,
            "bc": -5.0,
            "bp": -260,
            "soc": 80,
        }
        setpoint, flags = controller.calculate_setpoint(sys_data)

        assert isinstance(setpoint, int)
        assert isinstance(flags, str)

    def test_builds_system_state_and_calls_calculator(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        mock_victron.get_mppt_data.return_value = {"mppt0": {"w": 500.0, "a": 10.0}}
        mock_victron.get_pv_power.return_value = [300.0]
        mock_victron.get_inverter_power.return_value = -800
        mock_ha.get_vue_sensor.return_value = 100
        mock_ha.get_boolean.return_value = False
        controller.evcharger.read.return_value = {
            "ev_power": 100.0,
            "car_soc": 50,
            "ev_charging_kw": 0.1,
        }
        mock_calc.calculate.return_value = MagicMock(setpoint=-400, flags="", filtered_gt=50.0)

        sys_data = {
            "g1": 100,
            "g2": 50,
            "gt": 150,
            "t1": 200,
            "t2": 100,
            "tt": 300,
            "bv": 51.0,
            "bc": -4.0,
            "bp": -200,
            "soc": 75,
        }
        controller.calculate_setpoint(sys_data)

        mock_calc.calculate.assert_called_once()
        state_arg = mock_calc.calculate.call_args[0][0]
        assert isinstance(state_arg, SystemState)
        assert state_arg.g1 == 100
        assert state_arg.g2 == 50
        assert state_arg.gt == 150
        assert state_arg.t1 == 200
        assert state_arg.t2 == 100
        assert state_arg.tt == 300
        assert state_arg.inv_power == -800
        assert state_arg.mppt_total == 500.0
        assert state_arg.pv_inverter_total == 300.0
        assert state_arg.pv_total == 800.0
        assert state_arg.ev_power == 100

    def test_updates_filtered_gt_from_result(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        mock_victron.get_mppt_data.return_value = {}
        mock_victron.get_pv_power.return_value = []
        mock_victron.get_inverter_power.return_value = 0
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False
        mock_calc.calculate.return_value = MagicMock(setpoint=0, flags="", filtered_gt=42.0)

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

        assert controller.filtered_gt == 42.0

    def test_caches_mppt_and_pv_inverter_data(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        mock_victron.get_mppt_data.return_value = {"mppt0": {"w": 100.0, "a": 2.0}}
        mock_victron.get_pv_power.return_value = [200.0]
        mock_victron.get_inverter_power.return_value = 0
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False
        mock_calc.calculate.return_value = MagicMock(setpoint=0, flags="", filtered_gt=0.0)

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

        assert controller._cached_mppt_data == {"mppt0": {"w": 100.0, "a": 2.0}}
        assert controller._cached_pv_powers == [200.0]


class TestUpdateState(unittest.TestCase):
    """Test InverterController.update_state()"""

    def test_assembles_state_dict(self):
        controller, mock_victron, mock_ha, _mock_calc = _make_controller()
        mock_victron.get_battery_chain_socs.return_value = [80.0, 75.0]
        mock_victron.get_inverter_state.return_value = (9, "Inverting")
        mock_victron.get_ess_mode.return_value = {"is_external": True, "mode_name": "External"}
        mock_victron.get_all_batteries.return_value = []
        mock_victron.get_mppt_chargers.return_value = []
        mock_victron.get_battery_soc_local.return_value = 80.0
        mock_victron.get_battery_daily_energy.return_value = (10.0, 5.0)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = [15.0]
        mock_victron.get_pv_inverter_daily_yields.return_value = [3.0]
        mock_ha.get_all_booleans.return_value = {}
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False
        mock_ha.connected = False
        mock_ha.uptime = 0

        controller._cached_mppt_data = {"mppt0": {"w": 500.0, "a": 10.0}}
        controller._cached_pv_powers = [300.0]
        controller.filtered_gt = 25.0
        controller.previous_setpoint = -500

        sys_data = {
            "g1": 100,
            "g2": 50,
            "gt": 150,
            "t1": 200,
            "t2": 100,
            "tt": 300,
            "bv": 51.0,
            "bc": -4.0,
            "bp": -200,
            "soc": 80,
        }
        controller.update_state(sys_data, -450)

        state = controller.state
        assert state["setpoint"] == -450
        assert state["filtered_gt"] == 25.0
        assert state["mppt_total"] == 500.0
        assert state["pv_inverter_total"] == 300.0
        assert state["solar_total"] == 800.0
        assert state["inverter_state"] == "Inverting"
        assert state["battery_socs"] == [80.0, 75.0]
        assert state["ev_power"] == 0
        assert state["water_level"] is None
        assert state["ha_connected"] is False

    def test_injects_cached_mppt_data(self):
        controller, mock_victron, mock_ha, _ = _make_controller()
        mock_victron.get_battery_chain_socs.return_value = []
        mock_victron.get_inverter_state.return_value = (0, "Off")
        mock_victron.get_ess_mode.return_value = {"is_external": False}
        mock_victron.get_all_batteries.return_value = []
        mock_victron.get_mppt_chargers.return_value = []
        mock_victron.get_battery_soc_local.return_value = 0
        mock_victron.get_battery_daily_energy.return_value = (0, 0)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = []
        mock_victron.get_pv_inverter_daily_yields.return_value = []
        mock_ha.get_all_booleans.return_value = {}
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False

        controller._cached_mppt_data = {"mppt0": {"w": 100.0, "a": 2.0}}
        controller._cached_pv_powers = [50.0]

        sys_data = {
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
        controller.update_state(sys_data, 0)

        assert controller.state["mppt_data"] == {"mppt0": {"w": 100.0, "a": 2.0}}
        assert controller.state["pv_inverter_powers"] == [50.0]


class TestGetStateForMqtt(unittest.TestCase):
    """Slim inverter/state: Cerbo/dbus mirrors stripped; daemon extras kept."""

    def test_strips_migrated_cerbo_fields_keeps_daemon_extras(self):
        controller, mock_victron, mock_ha, _ = _make_controller()
        mock_victron.get_battery_chain_socs.return_value = [80.0]
        mock_victron.get_inverter_state.return_value = (9, "Inverting")
        mock_victron.get_ess_mode.return_value = {"is_external": True, "mode_name": "External"}
        mock_victron.get_all_batteries.return_value = [{"name": "Bank"}]
        mock_victron.get_mppt_chargers.return_value = [{"name": "MPPT"}]
        mock_victron.get_battery_soc_local.return_value = 78.0
        mock_victron.get_battery_daily_energy.return_value = (1.0, 2.0)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = [1.0]
        mock_victron.get_pv_inverter_daily_yields.return_value = [0.5]
        mock_ha.connected = True
        mock_ha.uptime = 42

        controller._cached_mppt_data = {"mppt0": {"w": 500.0, "a": 10.0}}
        controller._cached_pv_powers = [300.0]
        controller.filtered_gt = 25.0
        controller._internal_booleans = {"no_feed": True}

        sys_data = {
            "g1": 100,
            "g2": 50,
            "gt": 150,
            "t1": 200,
            "t2": 100,
            "tt": 300,
            "bv": 51.0,
            "bc": -4.0,
            "bp": -200,
            "_last_update": 123.0,
        }
        controller.update_state(sys_data, -450)
        out = controller.get_state_for_mqtt()

        # Migrated / Cerbo-owned — must not republish
        for key in (
            "g1",
            "g2",
            "gt",
            "t1",
            "t2",
            "tt",
            "loads",
            "batteries",
            "battery_soc",
            "battery_power",
            "battery_voltage",
            "battery_current",
            "solar_total",
            "mppt_total",
            "mppt_chargers",
            "setpoint",
            "inverter_state",
            "ev_power",
            "car_soc",
            "water_level",
            "pump_switch",
            "_last_update",
        ):
            assert key not in out, f"{key} should be stripped from inverter/state"

        # Daemon-owned extras — still published
        assert out["filtered_gt"] == 25.0
        assert out["booleans"]["no_feed"] is True
        assert out["ess_mode"]["is_external"] is True
        assert out["dry_run"] == controller.dry_run
        assert "daily_stats" in out
        assert "ui_config" in out
        assert "version" in out
        assert "uptime" in out
        assert out["ha_connected"] is True


class TestRunCycle(unittest.TestCase):
    """Test InverterController.run_cycle()"""

    def test_returns_true_on_success(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        mock_victron.get_system_data.return_value = {
            "g1": 0,
            "g2": 0,
            "gt": 0,
            "t1": 0,
            "t2": 0,
            "tt": 0,
            "bv": 48.0,
            "bc": 0,
            "bp": 0,
            "pv_total": 0,
            "soc": 80,
        }
        mock_victron.get_mppt_data.return_value = {}
        mock_victron.get_pv_power.return_value = []
        mock_victron.get_inverter_power.return_value = 0
        mock_victron.get_inverter_state.return_value = (9, "Inverting")
        mock_victron.get_battery_chain_socs.return_value = []
        mock_victron.get_ess_mode.return_value = {"is_external": True}
        mock_victron.get_all_batteries.return_value = []
        mock_victron.get_mppt_chargers.return_value = []
        mock_victron.get_battery_soc_local.return_value = 80
        mock_victron.get_battery_daily_energy.return_value = (0, 0)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = []
        mock_victron.get_pv_inverter_daily_yields.return_value = []
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False
        mock_ha.get_all_booleans.return_value = {}
        mock_ha.get_sensor.return_value = 0
        mock_calc.calculate.return_value = MagicMock(setpoint=0, flags="", filtered_gt=0.0)

        assert controller.run_cycle() is True

    def test_calls_get_system_data_and_set_grid_setpoint(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        sys_data = {
            "g1": 100,
            "g2": 50,
            "gt": 150,
            "t1": 200,
            "t2": 100,
            "tt": 300,
            "bv": 52.0,
            "bc": -5.0,
            "bp": -260,
            "pv_total": 0,
            "soc": 80,
        }
        mock_victron.get_system_data.return_value = sys_data
        mock_victron.get_mppt_data.return_value = {}
        mock_victron.get_pv_power.return_value = []
        mock_victron.get_inverter_power.return_value = 0
        mock_victron.get_inverter_state.return_value = (9, "Inverting")
        mock_victron.get_battery_chain_socs.return_value = []
        mock_victron.get_ess_mode.return_value = {"is_external": True}
        mock_victron.get_all_batteries.return_value = []
        mock_victron.get_mppt_chargers.return_value = []
        mock_victron.get_battery_soc_local.return_value = 80
        mock_victron.get_battery_daily_energy.return_value = (0, 0)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = []
        mock_victron.get_pv_inverter_daily_yields.return_value = []
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False
        mock_ha.get_all_booleans.return_value = {}
        mock_ha.get_sensor.return_value = 0
        mock_calc.calculate.return_value = MagicMock(setpoint=-400, flags="", filtered_gt=50.0)

        controller.run_cycle()

        mock_victron.get_system_data.assert_called_once()
        mock_victron.set_grid_setpoint.assert_called_once_with(-400)
        assert controller.previous_setpoint == -400

    def test_cycle_marks_watchdog_updates(self):
        controller, mock_victron, mock_ha, mock_calc = _make_controller()
        mock_victron.get_system_data.return_value = {
            "g1": 0,
            "g2": 0,
            "gt": 0,
            "t1": 0,
            "t2": 0,
            "tt": 0,
            "bv": 48.0,
            "bc": 0,
            "bp": 0,
            "pv_total": 0,
            "soc": 0,
        }
        mock_victron.get_mppt_data.return_value = {}
        mock_victron.get_pv_power.return_value = []
        mock_victron.get_inverter_power.return_value = 0
        mock_victron.get_inverter_state.return_value = (0, "Unknown")
        mock_victron.get_battery_chain_socs.return_value = []
        mock_victron.get_ess_mode.return_value = {"is_external": False}
        mock_victron.get_all_batteries.return_value = []
        mock_victron.get_mppt_chargers.return_value = []
        mock_victron.get_battery_soc_local.return_value = 0
        mock_victron.get_battery_daily_energy.return_value = (0, 0)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = []
        mock_victron.get_pv_inverter_daily_yields.return_value = []
        mock_ha.get_vue_sensor.return_value = 0
        mock_ha.get_boolean.return_value = False
        mock_ha.get_all_booleans.return_value = {}
        mock_ha.get_sensor.return_value = 0
        mock_calc.calculate.return_value = MagicMock(setpoint=0, flags="", filtered_gt=0.0)

        # Record timestamps before the cycle
        w = controller._watchdog
        pre_dbus = w._last_dbus_update
        pre_sp = w._last_setpoint_update

        controller.run_cycle()

        # Watchdog timestamps should have been updated during the cycle
        assert w._last_dbus_update >= pre_dbus
        assert w._last_setpoint_update >= pre_sp

    def test_returns_false_on_keyboard_interrupt(self):
        controller, mock_victron, _, _mock_calc = _make_controller()
        mock_victron.get_system_data.side_effect = KeyboardInterrupt

        assert controller.run_cycle() is False


class TestGetDailyStats(unittest.TestCase):
    """Test InverterController._get_daily_stats()"""

    def test_returns_correct_dict_structure(self):
        controller, mock_victron, _, _ = _make_controller()
        mock_victron.get_battery_daily_energy.return_value = (10.5, 8.2)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = [5.0, 3.0, 2.0]
        mock_victron.get_pv_inverter_daily_yields.return_value = [1.5, 0.5]

        stats = controller._get_daily_stats()

        assert "produced_today" in stats
        assert "battery_in" in stats
        assert "battery_out" in stats
        assert "mppt_daily" in stats
        assert "pv_inverter_daily" in stats
        assert "pv_total_daily" in stats
        assert stats["produced_today"] == 12.0  # sum([5,3,2]) + sum([1.5,0.5])
        assert stats["battery_in"] == 10.5
        assert stats["battery_out"] == 8.2

    def test_returns_zeroed_dict_when_no_data(self):
        controller, mock_victron, _, _ = _make_controller()
        mock_victron.get_battery_daily_energy.return_value = (0.0, 0.0)
        mock_victron.get_battery_yesterday_energy.return_value = (0.0, 0.0)
        mock_victron.get_mppt_daily_yields.return_value = []
        mock_victron.get_pv_inverter_daily_yields.return_value = []

        stats = controller._get_daily_stats()

        assert stats["produced_today"] == 0.0
        assert stats["battery_in"] == 0.0
        assert stats["battery_out"] == 0.0


class TestGetEvState(unittest.TestCase):
    """Test InverterController._get_ev_state() (dbus-evcharger / dbus-ev D-Bus reader)"""

    def test_returns_ev_data_when_enabled(self):
        controller, _mock_victron, _mock_ha, _ = _make_controller()
        controller.evcharger = MagicMock()
        controller.evcharger.read.return_value = {
            "ev_power": 1500.0,
            "car_soc": 85,
            "ev_charging_kw": 7.2,
        }

        with patch(f"{_MOD}.ENABLE_EV", True):
            state = controller._get_ev_state()

        assert state["ev_power"] == 1500
        assert state["car_soc"] == 85
        assert state["ev_charging_kw"] == 7.2

    def test_returns_zeros_when_disabled(self):
        with patch(f"{_MOD}.ENABLE_EV", False):
            controller, _, _, _ = _make_controller()
            state = controller._get_ev_state()
            assert state == {"ev_power": 0, "car_soc": 0, "ev_charging_kw": 0}

    def test_returns_zeros_when_no_reader(self):
        """No D-Bus reader (test mode): ev_power=0, no fake values."""
        with patch(f"{_MOD}.ENABLE_EV", True):
            controller, _, _, _ = _make_controller()
            controller.evcharger = None
            state = controller._get_ev_state()
            assert state == {"ev_power": 0, "car_soc": 0, "ev_charging_kw": 0}


class TestGetWaterState(unittest.TestCase):
    """Test InverterController._get_water_state() (dbus-pump D-Bus reader)"""

    def test_returns_reader_data_when_enabled(self):
        controller, _mock_victron, _mock_ha, _ = _make_controller()
        controller.water = MagicMock()
        controller.water.read.return_value = {
            "water_level": 45.0,
            "water_valve": True,
            "pump_switch": False,
        }

        with patch(f"{_MOD}.ENABLE_WATER", True):
            state = controller._get_water_state()

        assert state["water_level"] == 45.0
        assert state["water_valve"] is True
        assert state["pump_switch"] is False

    def test_returns_none_when_no_reader(self):
        with patch(f"{_MOD}.ENABLE_WATER", True):
            controller, _, _, _ = _make_controller()
            state = controller._get_water_state()
            assert state == {"water_level": None, "water_valve": None, "pump_switch": None}

    def test_returns_none_when_disabled(self):
        with patch(f"{_MOD}.ENABLE_WATER", False):
            controller, _, _, _ = _make_controller()
            state = controller._get_water_state()
            assert state == {"water_level": None, "water_valve": None, "pump_switch": None}


class TestGetHaState(unittest.TestCase):
    """Test InverterController._get_ha_state()"""

    def test_returns_ha_data_when_enabled(self):
        with patch(f"{_MOD}.ENABLE_HA", True):
            controller, _mock_victron, mock_ha, _ = _make_controller()
            mock_ha.connected = True
            mock_ha.uptime = 3600

            # Set the internal booleans
            controller.set_boolean("only_charging", True)

            state = controller._get_ha_state()

        assert state["booleans"] == {
            "only_charging": True,
            "no_feed": False,
            "house_support": False,
            "charge_battery": False,
            "do_not_supply_charger": False,
            "set_limit_to_ev_charger": False,
            "minimize_charging": False,
        }
        # laundry_outlet, home_recliner, home_garage removed — HA no longer
        # polled for these; relay/switch control lives in the controller.
        assert "laundry_outlet" not in state
        assert "home_recliner" not in state
        assert "home_garage" not in state
        assert state["ha_connected"] is True
        assert state["ha_uptime"] == 3600

    def test_returns_zeros_when_disabled(self):
        with patch(f"{_MOD}.ENABLE_HA", False):
            controller, _, _, _ = _make_controller()
            state = controller._get_ha_state()
            assert state["booleans"] == {
                "only_charging": False,
                "no_feed": False,
                "house_support": False,
                "charge_battery": False,
                "do_not_supply_charger": False,
                "set_limit_to_ev_charger": False,
                "minimize_charging": False,
            }
            assert state["ha_connected"] is False
            assert state["ha_uptime"] == 0


class TestSetLoopInterval(unittest.TestCase):
    """Test InverterController.set_loop_interval()"""

    def test_clamps_to_valid_range(self):
        controller, _, _, _ = _make_controller()
        assert controller.set_loop_interval(0.01) == 0.1
        assert controller.set_loop_interval(10.0) == 5.0
        assert controller.set_loop_interval(1.0) == 1.0


class TestSetPowerLimits(unittest.TestCase):
    """Test InverterController.set_power_limits()"""

    def test_updates_limits_and_calculator(self):
        controller, _, _, mock_calc = _make_controller()
        result = controller.set_power_limits(-1000, 1000)
        assert result["min"] == -1000
        assert result["max"] == 1000
        assert mock_calc.power_limit_min == -1000
        assert mock_calc.power_limit_max == 1000

    def test_clamps_to_absolute_limits(self):
        controller, _, _, _ = _make_controller()
        result = controller.set_power_limits(-5000, 5000)
        assert result["min"] == -3000
        assert result["max"] == 3000


class TestToggleDryRun(unittest.TestCase):
    """Test InverterController.toggle_dry_run()"""

    def test_toggles_dry_run(self):
        controller, _, _, _ = _make_controller(dry_run=False)
        assert controller.toggle_dry_run() is True
        assert controller.toggle_dry_run() is False


class TestSetManualSetpoint(unittest.TestCase):
    """Test InverterController.set_manual_setpoint()"""

    def test_sets_clamped_setpoint(self):
        controller, _, _, _ = _make_controller()
        controller.set_manual_setpoint(500)
        assert controller.manual_setpoint == 500

    def test_clamps_to_limits(self):
        controller, _, _, _ = _make_controller()
        controller.set_manual_setpoint(5000)
        assert controller.manual_setpoint == 2250


class TestNextSlot:
    """Deadline-anchored pacing helper."""

    def test_early_cycle_sleeps_remainder(self):
        # 100ms left in the slot -> sleep 100ms, deadline advances one slot
        with patch("main.time.monotonic", return_value=1000.0):
            delay, deadline = main._next_slot(1000.1, 0.33)
        assert delay == pytest.approx(0.1)
        assert deadline == pytest.approx(1000.43)

    def test_slow_cycle_skips_sleep(self):
        # Slightly past deadline -> no sleep, still advance one slot
        with patch("main.time.monotonic", return_value=1010.1):
            delay, deadline = main._next_slot(1010.0, 0.33)
        assert delay == 0.0
        assert deadline == pytest.approx(1010.33)

    def test_stall_realigns_instead_of_catchup(self):
        # Stalled past a whole slot -> realign, don't fire backlog cycles
        with patch("main.time.monotonic", return_value=1012.0):
            delay, deadline = main._next_slot(1010.0, 0.33)
        assert delay == 0.0
        assert deadline == pytest.approx(1012.33)


if __name__ == "__main__":
    unittest.main()

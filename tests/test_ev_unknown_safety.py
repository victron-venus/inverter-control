"""Unknown EV power respects explicit modes without disabling house support."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from inverter_control import background_reader
from inverter_control.background_reader import BackgroundReader
from inverter_control.logic import SetpointCalculator, SystemState
from tests.test_main import _make_controller


def system_state(**changes):
    values = {
        "g1": 8182,
        "g2": 0,
        "gt": 8182,
        "t1": 0,
        "t2": 0,
        "tt": 0,
        "inv_power": -1462,
        "mppt_total": 1533,
        "pv_inverter_total": 549,
        "pv_total": 2082,
        "ev_power": None,
        "garage_power": 0,
        "only_charging": True,
        "no_feed": False,
        "house_support": False,
        "charge_battery": False,
        "do_not_supply_charger": True,
        "limit_to_ev": False,
        "previous_setpoint": -1462,
        "prefiltered_gt": 8182,
    }
    values.update(changes)
    return SystemState(**values)


@pytest.mark.parametrize("power", [None, True, False, float("nan"), float("inf"), -1, "9300"])
def test_unknown_and_invalid_site_power_in_only_charging_never_request_output(power):
    result = SetpointCalculator({}).calculate(system_state(ev_power=power))
    assert result.setpoint == 0
    assert "[NoEV:site-unknown]" in result.flags
    assert "[CHG]" not in result.flags


@pytest.mark.parametrize(
    "mode",
    [
        {},
        {"limit_to_ev": True, "garage_power": 1500},
        {"house_support": True, "pv_inverter_total": 0},
    ],
)
def test_safety_floor_wins_after_higher_modes_slew_and_negative_only_limits(mode):
    calculator = SetpointCalculator({"SETPOINT_DELTA_LIMIT": 100, "POWER_LIMIT_MAX": -100})
    result = calculator.calculate(system_state(previous_setpoint=-2300, **mode))
    assert result.setpoint == 0
    assert "[NoEV:limits-conflict]" in result.flags


def test_explicit_charge_mode_remains_positive_without_safety_forcing_it():
    result = SetpointCalculator({}).calculate(
        system_state(charge_battery=True, previous_setpoint=2200)
    )
    assert result.setpoint == 2200
    assert "[CHG]" in result.flags


def test_known_zero_and_noev_disabled_preserve_ordinary_control():
    known_zero = SetpointCalculator({}).calculate(system_state(ev_power=0))
    disabled = SetpointCalculator({}).calculate(
        system_state(ev_power=None, do_not_supply_charger=False, limit_to_ev=True)
    )
    assert known_zero.setpoint == disabled.setpoint == -1390
    assert "site-unknown" not in known_zero.flags + disabled.flags


@pytest.mark.parametrize("noev", [False, True])
@pytest.mark.parametrize("only_charging", [False, True])
@pytest.mark.parametrize("no_feed", [False, True])
def test_unknown_mode_matrix_preserves_house_support_unless_explicitly_restricted(
    noev, only_charging, no_feed
):
    mode = {
        "do_not_supply_charger": noev,
        "only_charging": only_charging,
        "no_feed": no_feed,
    }
    result = SetpointCalculator({}).calculate(system_state(**mode))
    ordinary = SetpointCalculator({}).calculate(system_state(ev_power=0, **mode))
    if noev and (only_charging or no_feed):
        assert result.setpoint >= 0
        assert "[NoEV:site-unknown]" in result.flags
    else:
        assert result.setpoint == ordinary.setpoint
        assert result.filtered_gt == ordinary.filtered_gt
        if not (only_charging or no_feed):
            assert result.setpoint < 0
        assert ("[NoEV:exclusion-unavailable]" in result.flags) is noev


@pytest.mark.parametrize("power", [None, True, False, float("nan"), float("inf"), -1, "9300"])
def test_invalid_site_reading_does_not_disable_ordinary_house_support(power):
    result = SetpointCalculator({}).calculate(system_state(ev_power=power, only_charging=False))
    assert result.setpoint < 0
    assert "[NoEV:exclusion-unavailable]" in result.flags
    assert "[NoEV:site-unknown]" not in result.flags


@pytest.fixture
def rig(no_network, monkeypatch):
    controller, victron, _, _ = _make_controller()
    controller.calculator = SetpointCalculator({})
    controller.previous_setpoint = -1462
    controller.filtered_gt = None
    controller.grid_filter = None
    controller.submeter_trim.enabled = False
    controller._control_flags["only_charging"] = True
    controller._control_flags["do_not_supply_charger"] = True
    controller.evcharger.read.return_value = {"ev_power": None}
    controller._grid_ready_for_control = MagicMock(return_value=True)
    controller._skip_changed_grid = MagicMock(return_value=False)
    controller._update_dvcc_limits = MagicMock()
    controller.handle_minimize_charging = MagicMock()
    controller._render_cycle_telemetry = MagicMock()
    monkeypatch.setattr("inverter_control.controller.ENABLE_GRID_SMOOTHING_WITH_HOME", False)
    data = {"g1": 8182, "g2": 0, "gt": 8182, "t1": 0, "t2": 0, "tt": 0}
    victron.get_system_data.return_value = data
    victron.get_grid_status.return_value = data
    victron.get_mppt_data.return_value = {"pv": {"w": 1533}}
    victron.get_pv_power.return_value = [549]
    victron.get_inverter_power.return_value = -1462
    victron.set_grid_setpoint.return_value = True
    return SimpleNamespace(controller=controller, victron=victron, data=data)


def test_unknown_reaches_calculator_and_accepted_zero_becomes_baseline(rig):
    calculate = MagicMock(wraps=rig.controller.calculator.calculate)
    rig.controller.calculator.calculate = calculate
    assert rig.controller.run_cycle()
    assert calculate.call_args.args[0].ev_power is None
    rig.victron.set_grid_setpoint.assert_called_once_with(0)
    assert rig.controller.previous_setpoint == 0


def test_unknown_reaches_calculator_and_house_output_is_written_when_modes_off(rig):
    rig.controller.set_control_flag("only_charging", False)
    assert rig.controller.run_cycle()
    output = rig.victron.set_grid_setpoint.call_args.args[0]
    assert output < 0
    assert rig.controller.previous_setpoint == output
    flags = rig.controller._render_cycle_telemetry.call_args.args[3]
    assert "[NoEV:exclusion-unavailable]" in flags
    assert "[NoEV:site-unknown]" not in flags


def test_backup_sample_hold_cannot_replay_negative_output_when_ev_unknown(rig):
    rig.data.update(_grid_backup=True, _grid_measurement_time=123)
    rig.controller._last_backup_measurement = 123
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(0)
    flags = rig.controller._render_cycle_telemetry.call_args.args[3]
    assert "[SUBMETER HOLD]" in flags
    assert "[NoEV:site-unknown]" in flags


@pytest.mark.parametrize("race", ["power_disappears", "flag_turns_on", "negative_only_limits"])
def test_final_write_revalidates_availability_flag_and_limits(rig, race):
    if race == "power_disappears":
        rig.controller.evcharger.read.return_value = {"ev_power": 0}
    if race == "flag_turns_on":
        rig.controller._control_flags["do_not_supply_charger"] = False

    def before_write():
        if race == "power_disappears":
            rig.controller.evcharger.read.return_value = {"ev_power": None}
        elif race == "flag_turns_on":
            rig.controller.set_control_flag("do_not_supply_charger", True)
        else:
            rig.controller.set_power_limits(-2300, -100)
        return rig.data

    rig.victron.get_grid_status.side_effect = before_write
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(0)
    assert rig.controller.previous_setpoint == 0
    if race == "negative_only_limits":
        assert "limits-conflict" in rig.controller._render_cycle_telemetry.call_args.args[3]


@pytest.mark.parametrize("mode", ["only_charging", "no_feed"])
def test_restrictive_mode_enabled_after_calculation_is_checked_at_write(rig, mode):
    rig.controller.set_control_flag("only_charging", False)

    def before_write():
        rig.controller.set_control_flag(mode, True)
        return rig.data

    rig.victron.get_grid_status.side_effect = before_write
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(0)
    flags = rig.controller._render_cycle_telemetry.call_args.args[3]
    assert "[NoEV:site-unknown]" in flags
    assert "exclusion-unavailable" not in flags


@pytest.mark.parametrize("mode", ["only_charging", "no_feed"])
def test_disabled_restrictive_mode_restores_house_support_next_cycle(rig, mode):
    rig.controller.set_control_flag("only_charging", mode == "only_charging")
    rig.controller.set_control_flag("no_feed", mode == "no_feed")

    def before_write():
        rig.controller.set_control_flag(mode, False)
        return rig.data

    rig.victron.get_grid_status.side_effect = before_write
    assert rig.controller.run_cycle()
    # A previously restricted candidate is not retroactively reconstructed at
    # the write boundary. The next calculation uses the disabled mode.
    flags = rig.controller._render_cycle_telemetry.call_args.args[3]
    assert "[NoEV:exclusion-unavailable]" in flags
    assert "[NoEV:site-unknown]" not in flags
    assert rig.controller.run_cycle()
    assert rig.victron.set_grid_setpoint.call_args.args[0] < 0


def test_recovery_before_write_clears_obsolete_unknown_diagnostic(rig):
    def before_write():
        rig.controller.evcharger.read.return_value = {"ev_power": 9000}
        return rig.data

    rig.victron.get_grid_status.side_effect = before_write
    assert rig.controller.run_cycle()
    flags = rig.controller._render_cycle_telemetry.call_args.args[3]
    assert "site-unknown" not in flags
    assert "exclusion-unavailable" not in flags


def test_background_age_expiry_before_write_is_unknown_without_synchronous_io(rig, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(background_reader, "monotonic", lambda: clock[0])
    refresh = MagicMock(side_effect=AssertionError("must not read hardware in write gate"))
    reader = BackgroundReader(refresh, {"ev_power": None}, name="test-ev")
    reader._snapshot = (100.0, {"ev_power": 0})
    rig.controller.evcharger = reader

    def before_write():
        clock[0] = 104.0
        return rig.data

    rig.victron.get_grid_status.side_effect = before_write
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(0)
    refresh.assert_not_called()


def test_measured_zero_recovers_ordinary_control_after_unknown(rig):
    assert rig.controller.run_cycle()
    assert rig.controller.previous_setpoint == 0
    rig.controller.evcharger.read.return_value = {"ev_power": 0}
    assert rig.controller.run_cycle()
    assert rig.victron.set_grid_setpoint.call_args.args[0] < 0
    assert "site-unknown" not in rig.controller._render_cycle_telemetry.call_args.args[3]


def test_explicit_one_shot_manual_command_keeps_its_ownership(rig):
    rig.controller.manual_setpoint = -900
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(-900)
    assert rig.controller.previous_setpoint == -900
    assert rig.controller.manual_setpoint is None


def test_persistent_manual_override_keeps_its_ownership(rig):
    result = rig.controller.set_setpoint_override(-900)
    assert result["last_error"] is None
    rig.victron.set_grid_setpoint.reset_mock()
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_not_called()
    assert rig.controller.get_setpoint_override()["value"] == -900
    assert rig.controller.state["grid_control_reason"] == "Manual setpoint override active"

"""Exercise slow trim through the real calculator, controller and write gate."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from inverter_control import config
from inverter_control import controller as controller_module
from inverter_control.control_flags import CONTROL_FLAG_KEYS
from inverter_control.logic import SetpointCalculator
from tests.test_main import _make_controller

SERVICE = "com.victronenergy.acload.integration_submeter"
PRIMARY = "com.victronenergy.grid.integration_primary"


@pytest.fixture
def rig(monkeypatch, no_network):
    """Keep all physical services mocked but retain both control state machines."""
    clock = SimpleNamespace(elapsed=0.0)
    clock.monotonic = lambda: 1000.0 + clock.elapsed
    clock.wall = lambda: 1_800_000_000.0 + clock.elapsed
    monkeypatch.setattr(controller_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(controller_module.time, "time", clock.wall)
    monkeypatch.setattr(config, "SUBMETER_TRIM_ENABLED", True)
    monkeypatch.setattr(config, "GRID_BACKUP_SERVICE", SERVICE)
    monkeypatch.setattr(config, "CREEP_RATE", 0)
    monkeypatch.setattr(controller_module, "ENABLE_GRID_SMOOTHING_WITH_HOME", False)
    monkeypatch.setattr(controller_module, "broadcast_line", MagicMock())
    controller, victron, ha, _ = _make_controller()
    controller.calculator = SetpointCalculator({"CREEP_RATE": 0})
    controller.previous_setpoint = -600
    controller.current_setpoint = -600
    controller.filtered_gt = 0.0
    controller.grid_filter = MagicMock()
    controller.grid_filter.value.return_value = 0.0
    controller._control_grid_selection = 1
    controller._control_history_generation = controller._watchdog.control_generation()
    controller.update_state = MagicMock()
    controller._in_expensive_window = MagicMock(return_value=False)
    controller.console.format_line.return_value = "test cycle"
    victron.get_mppt_data.return_value = {}
    victron.get_pv_power.return_value = []
    victron.get_inverter_power.return_value = -600
    victron.get_ess_mode.return_value = {"is_external": True}
    victron.set_grid_setpoint.return_value = True
    ha.get_vue_sensor.return_value = 0
    ha.get_all_vue_sensors.return_value = {}
    ha.get_boolean.return_value = False

    def snapshot(power=24.0, raw=0.0, **overrides):
        data = {
            "g1": raw,
            "g2": 0.0,
            "gt": raw,
            "t1": 600.0,
            "t2": 0.0,
            "tt": 600.0,
            "bv": 52.0,
            "bc": -12.0,
            "bp": -624.0,
            "soc": 80.0,
            "_grid_valid": True,
            "_grid_primary_valid": True,
            "_grid_backup": False,
            "_grid_source": PRIMARY,
            "_grid_source_instance": 30,
            "_grid_selection_generation": 1,
            "_grid_measurement_time": None,
            "_grid_backup_status": {
                "available": True,
                "service": SERVICE,
                "device_instance": 42,
                "generation": 1,
                "measurement_time": clock.wall(),
                "power": power,
                "age_seconds": 0.0,
            },
        }
        data.update(overrides)
        return data

    def cycle(elapsed, power=24.0, raw=0.0, before_write=None, **overrides):
        clock.elapsed = float(elapsed)
        data = snapshot(power, raw, **overrides)
        victron.get_system_data.return_value = data

        def current_grid():
            current = deepcopy(data)
            if before_write:
                before_write(current)
            return current

        victron.get_grid_status.side_effect = current_grid
        assert controller.run_cycle() is True
        assert controller.submeter_trim.status()["reason"] != "cycle_error"
        return data

    def warm(power=24.0, until=15):
        for elapsed in range(0, until + 1, 3):
            cycle(elapsed, power)

    return SimpleNamespace(
        controller=controller,
        victron=victron,
        clock=clock,
        snapshot=snapshot,
        cycle=cycle,
        warm=warm,
    )


@pytest.mark.parametrize("power,expected", [(24.0, -604), (-24.0, -596)])
def test_five_fresh_reports_move_held_command_once_after_settling(rig, power, expected):
    rig.warm(power)
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["sample_count"] == 4
    data = rig.cycle(18, power)
    assert rig.controller.previous_setpoint == expected
    assert rig.victron.set_grid_setpoint.call_args.args == (expected,)
    assert rig.controller.submeter_trim.status()["total_trim"] == expected + 600
    # Ten controller reads of one source report cannot become ten corrections.
    for index in range(10):
        rig.cycle(18 + index * 0.33, power, _grid_backup_status=data["_grid_backup_status"])
    assert rig.controller.previous_setpoint == expected


def test_raw_derivative_noise_does_not_bypass_deadband_hold(rig):
    for index, elapsed in enumerate(range(0, 19, 3)):
        rig.cycle(elapsed, raw=30.0 if index % 2 else -30.0)
        flags = rig.controller.console.format_line.call_args.args[3]
        assert "[D:" not in flags
        assert "[B:" not in flags
    assert rig.controller.previous_setpoint == -604
    assert "[TRIM:-4]" in flags


def test_disabled_opt_in_preserves_existing_calculator_behavior(rig):
    rig.controller.submeter_trim.enabled = False
    rig.warm(until=30)
    assert rig.controller.previous_setpoint == -600
    assert rig.controller._trim_decision is None
    assert all(call.args == (-600,) for call in rig.victron.set_grid_setpoint.call_args_list)


def test_limit_edit_discards_an_unwritten_zero_delta_trim_proposal(rig):
    rig.controller.previous_setpoint = 2000
    rig.controller.current_setpoint = 2000
    rig.victron.get_inverter_power.return_value = 2000
    commit = MagicMock(wraps=rig.controller.submeter_trim.commit)
    rig.controller.submeter_trim.commit = commit
    rig.cycle(0, before_write=lambda _: rig.controller.set_power_limits(-1000, 1000))
    assert rig.victron.set_grid_setpoint.call_args.args == (1000,)
    assert rig.controller.previous_setpoint == 1000
    assert rig.controller._trim_decision is None
    commit.assert_not_called()
    assert rig.controller.submeter_trim.status()["last_accepted_setpoint"] == 2000
    rig.cycle(3)
    commit.assert_called_once()
    assert rig.controller.submeter_trim.status()["last_accepted_setpoint"] == 1000


@pytest.mark.parametrize("flag", CONTROL_FLAG_KEYS)
def test_every_operating_mode_excludes_slow_trim(rig, flag):
    rig.warm()
    rig.controller.set_control_flag(flag, True)
    rig.clock.elapsed = 18
    _, flags = rig.controller.calculate_setpoint(rig.snapshot())
    assert "[TRIM:" not in flags
    assert rig.controller.submeter_trim.status()["reason"] == "operating_mode"
    assert rig.controller.submeter_trim.status()["sample_count"] == 0


def test_one_shot_precharge_request_blocks_trim(rig):
    rig.warm()
    rig.controller._pre_charge_requested = True
    rig.controller._pre_charge_expires_at = rig.clock.wall() + 300
    rig.clock.elapsed = 18
    _, flags = rig.controller.calculate_setpoint(rig.snapshot())
    assert not rig.controller._pre_charge_requested
    assert "[TRIM:" not in flags
    assert rig.controller.submeter_trim.status()["reason"] == "operating_mode"


@pytest.mark.parametrize("gate", ["manual", "override", "ess", "creep", "derived", "unpinned"])
def test_non_normal_feedback_context_blocks_trim(rig, monkeypatch, gate):
    rig.warm()
    if gate == "manual":
        rig.controller.set_manual_setpoint(-600)
    elif gate == "override":
        monkeypatch.setattr(rig.controller, "get_setpoint_override", lambda: {"value": -600})
    elif gate == "ess":
        rig.victron.get_ess_mode.return_value = {"is_external": False}
    elif gate == "creep":
        monkeypatch.setattr(config, "CREEP_RATE", 0.5)
    elif gate == "derived":
        monkeypatch.setattr(controller_module, "ENABLE_GRID_SMOOTHING_WITH_HOME", True)
        rig.controller.ha.get_all_vue_sensors.return_value = {"total": 0}
    else:
        monkeypatch.setattr(config, "GRID_BACKUP_SERVICE", "")
    rig.clock.elapsed = 18
    _, flags = rig.controller.calculate_setpoint(rig.snapshot())
    assert "[TRIM:" not in flags
    assert rig.controller.submeter_trim.status()["sample_count"] == 0


def test_enabled_home_smoothing_without_total_sensor_allows_trim(rig, monkeypatch):
    monkeypatch.setattr(controller_module, "ENABLE_GRID_SMOOTHING_WITH_HOME", True)
    rig.controller.ha.get_all_vue_sensors.return_value = {"garage": 500}
    rig.warm(until=18)
    assert rig.controller.previous_setpoint == -604
    assert rig.controller.submeter_trim.status()["total_trim"] == -4


@pytest.mark.parametrize("total", [0, 600, None, float("nan"), float("inf")])
def test_present_home_total_blocks_trim_even_when_zero_or_unknown(rig, monkeypatch, total):
    monkeypatch.setattr(controller_module, "ENABLE_GRID_SMOOTHING_WITH_HOME", True)
    rig.controller.ha.get_all_vue_sensors.return_value = {"total": total}
    rig.warm(until=18)
    assert rig.controller.previous_setpoint == -600
    status = rig.controller.submeter_trim.status()
    assert status["reason"] == "incompatible_feedback"
    assert status["sample_count"] == 0


def test_home_total_discovered_before_write_cancels_trim(rig, monkeypatch):
    monkeypatch.setattr(controller_module, "ENABLE_GRID_SMOOTHING_WITH_HOME", True)
    rig.warm()
    rig.victron.set_grid_setpoint.reset_mock()

    def discover_total(_):
        rig.controller.ha.get_all_vue_sensors.return_value = {"total": 0}

    rig.cycle(18, before_write=discover_total)
    rig.victron.set_grid_setpoint.assert_not_called()
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["total_trim"] == 0


def test_absent_home_total_requires_new_window_after_present_period(rig, monkeypatch):
    monkeypatch.setattr(controller_module, "ENABLE_GRID_SMOOTHING_WITH_HOME", True)
    rig.warm()
    rig.controller.ha.get_all_vue_sensors.return_value = {"total": 0}
    rig.cycle(18)
    assert rig.controller.submeter_trim.status()["sample_count"] == 0
    rig.controller.ha.get_all_vue_sensors.return_value = {}
    for elapsed in (21, 24, 27, 30, 33, 36):
        rig.cycle(elapsed)
        assert rig.controller.previous_setpoint == -600
    rig.cycle(39)
    assert rig.controller.previous_setpoint == -604


@pytest.mark.parametrize(
    "field,value",
    [
        ("_grid_valid", False),
        ("_grid_primary_valid", False),
        ("_grid_backup", True),
    ],
)
def test_invalid_or_backup_source_excludes_trim(rig, field, value):
    rig.warm()
    rig.clock.elapsed = 18
    _, flags = rig.controller.calculate_setpoint(rig.snapshot(**{field: value}))
    assert "[TRIM:" not in flags
    assert rig.controller.submeter_trim.status()["sample_count"] == 0


def test_rejected_write_cannot_commit_trim_or_applied_baseline(rig):
    rig.warm()
    rig.victron.set_grid_setpoint.return_value = False
    rig.cycle(18)
    assert rig.victron.set_grid_setpoint.call_args.args == (-604,)
    assert rig.controller.previous_setpoint == -600
    status = rig.controller.submeter_trim.status()
    assert status["total_trim"] == 0
    assert status["last_trim_time"] is None
    assert status["reason"] == "write_rejected"


@pytest.mark.parametrize(
    "change",
    [
        "timestamp",
        "primary_identity",
        "primary_generation",
        "backup_generation",
        "backup_identity",
        "backup_instance",
        "unavailable",
        "stale",
        "primary_step",
        "nonfinite_primary",
        "mode",
        "mode_roundtrip",
        "precharge",
        "manual",
        "ess",
        "limit",
        "delta_limit",
    ],
)
def test_before_write_edges_cancel_pending_trim(rig, change):
    rig.warm()
    rig.victron.set_grid_setpoint.reset_mock()

    def mutate(data):
        sample = data["_grid_backup_status"]
        if change == "timestamp":
            sample["measurement_time"] += 0.1
        elif change == "primary_identity":
            data["_grid_source_instance"] += 1
        elif change == "primary_generation":
            data["_grid_selection_generation"] += 1
        elif change == "backup_generation":
            sample["generation"] += 1
        elif change == "backup_identity":
            sample["service"] = "com.victronenergy.acload.foreign"
        elif change == "backup_instance":
            sample["device_instance"] += 1
        elif change == "unavailable":
            sample["available"] = False
        elif change == "stale":
            sample["age_seconds"] = 20
        elif change == "primary_step":
            data["gt"] = 120.0
        elif change == "nonfinite_primary":
            data["gt"] = float("nan")
        elif change == "mode":
            rig.controller.set_control_flag("no_feed", True)
        elif change == "mode_roundtrip":
            rig.controller.set_control_flag("no_feed", True)
            rig.controller.set_control_flag("no_feed", False)
        elif change == "precharge":
            rig.controller._pre_charge_requested = True
            rig.controller._pre_charge_expires_at = rig.clock.wall() + 300
        elif change == "manual":
            rig.controller.set_manual_setpoint(-500)
        elif change == "ess":
            rig.victron.get_ess_mode.return_value = {"is_external": False}
        elif change == "limit":
            rig.controller.set_power_limits(-602, 2250)
        else:
            rig.controller.calculator.delta_limit = 2

    rig.cycle(18, before_write=mutate)
    rig.victron.set_grid_setpoint.assert_not_called()
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["total_trim"] == 0


def test_same_name_submeter_restart_needs_a_whole_new_window(rig):
    rig.warm()
    rig.clock.elapsed = 18
    sample = rig.snapshot()["_grid_backup_status"]
    sample["generation"] = 2
    rig.cycle(18, _grid_backup_status=sample)
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["sample_count"] == 0
    for elapsed in (21, 24, 27, 30, 33):
        rig.clock.elapsed = elapsed
        sample = rig.snapshot()["_grid_backup_status"]
        sample["generation"] = 2
        rig.cycle(elapsed, _grid_backup_status=sample)
    assert rig.controller.previous_setpoint == -600
    rig.clock.elapsed = 36
    sample = rig.snapshot()["_grid_backup_status"]
    sample["generation"] = 2
    rig.cycle(36, _grid_backup_status=sample)
    assert rig.controller.previous_setpoint == -604


@pytest.mark.parametrize("limit", ["power", "delta"])
def test_trim_cannot_bypass_normal_safety_limits(rig, limit):
    if limit == "power":
        rig.controller.set_power_limits(-602, 2250)
    else:
        rig.controller.calculator.delta_limit = 2
    rig.warm(until=18)
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["total_trim"] == 0


def test_dry_run_to_live_discards_historical_window(rig):
    rig.controller.set_dry_run(True)
    rig.warm()
    rig.victron.set_grid_setpoint.assert_not_called()
    rig.controller.set_dry_run(False)
    rig.cycle(18)
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["sample_count"] == 0
    assert rig.victron.set_grid_setpoint.call_args.args == (-600,)


def test_mode_roundtrip_between_cycles_discards_historical_window(rig):
    rig.warm()
    rig.controller.set_control_flag("no_feed", True)
    rig.controller.set_control_flag("no_feed", False)
    rig.cycle(18)
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["sample_count"] == 0


def test_primary_loss_pauses_real_cycle_without_writing_trim(rig):
    rig.warm()
    rig.victron.set_grid_setpoint.reset_mock()
    rig.cycle(18, _grid_valid=False)
    rig.victron.set_grid_setpoint.assert_not_called()
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["sample_count"] == 0


def test_fast_load_response_wins_and_forces_new_trim_window(rig):
    rig.warm()
    rig.controller.grid_filter.value.return_value = 120.0
    rig.cycle(18, raw=120.0)
    flags = rig.controller.console.format_line.call_args.args[3]
    assert "[TRIM:" not in flags
    assert rig.controller.previous_setpoint < -600
    assert rig.controller.submeter_trim.status()["sample_count"] == 0
    assert rig.controller.submeter_trim.status()["total_trim"] == 0
    accepted = rig.controller.previous_setpoint
    rig.controller.grid_filter.value.return_value = 0.0
    rig.cycle(21)
    assert rig.controller.previous_setpoint == accepted


def test_stalled_primary_write_gate_cannot_accept_aged_submeter_proposal(rig):
    rig.warm()
    rig.victron.set_grid_setpoint.reset_mock()

    def slow_read(_):
        rig.clock.elapsed += 9

    rig.cycle(18, before_write=slow_read)
    rig.victron.set_grid_setpoint.assert_not_called()
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["total_trim"] == 0


def test_mode_roundtrip_during_proposal_cannot_escape_write_context(rig, monkeypatch):
    rig.warm()
    propose = rig.controller.submeter_trim.propose

    def proposal_with_interleaved_mode_change(**kwargs):
        decision = propose(**kwargs)
        if decision.delta:
            rig.controller.set_control_flag("no_feed", True)
            rig.controller.set_control_flag("no_feed", False)
        return decision

    monkeypatch.setattr(
        rig.controller.submeter_trim, "propose", proposal_with_interleaved_mode_change
    )
    rig.victron.set_grid_setpoint.reset_mock()
    rig.cycle(18)
    rig.victron.set_grid_setpoint.assert_not_called()
    assert rig.controller.previous_setpoint == -600
    assert rig.controller.submeter_trim.status()["total_trim"] == 0


@pytest.mark.parametrize("primary,submeter", [(-49.0, 24.0), (29.0, -24.0)])
def test_trim_cannot_push_primary_past_its_fast_control_deadband(rig, primary, submeter):
    rig.controller.grid_filter.value.return_value = primary
    for elapsed in range(0, 19, 3):
        rig.cycle(elapsed, power=submeter, raw=primary)
    assert rig.controller.previous_setpoint == -600
    assert rig.victron.set_grid_setpoint.call_args.args == (-600,)
    status = rig.controller.submeter_trim.status()
    assert status["reason"] == "primary_deadband_limit"
    assert status["total_trim"] == 0
    flags = rig.controller.console.format_line.call_args.args[3]
    assert "[TRIM:" not in flags


@pytest.mark.parametrize("primary,submeter,expected", [(-20.0, 24.0, -604), (20.0, -24.0, -596)])
def test_trim_inside_primary_deadband_retains_full_small_correction(
    rig, primary, submeter, expected
):
    rig.controller.grid_filter.value.return_value = primary
    for elapsed in range(0, 19, 3):
        rig.cycle(elapsed, power=submeter, raw=primary)
    assert rig.controller.previous_setpoint == expected
    assert rig.victron.set_grid_setpoint.call_args.args == (expected,)
    assert rig.controller.submeter_trim.status()["total_trim"] == expected + 600

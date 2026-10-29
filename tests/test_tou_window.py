"""TOU guards forced charging across all transports without control-thread I/O."""

import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from inverter_control.background_reader import BackgroundReader
from inverter_control.controller import InverterController
from inverter_control.logic import SetpointCalculator
from inverter_control.victron import TOU_END_SETTING, TOU_START_SETTING, VictronDBus


def _make_controller(start=15, end=24):
    ctrl = InverterController.__new__(InverterController)
    ctrl.victron = MagicMock()
    ctrl.tou = MagicMock()
    ctrl.tou.read.return_value = {"start": start, "end": end, "timezone": UTC}
    return ctrl


@pytest.mark.parametrize(
    "start,end,blocked_hours",
    [
        (15, 24, range(15, 24)),
        (22, 6, (*range(22, 24), *range(6))),
        (24, 6, range(6)),
        (0, 24, range(24)),
        (8, 8, ()),
        (-1, -1, ()),
        (15, -1, ()),
    ],
)
def test_window_boundaries_and_disabled_hours(start, end, blocked_hours):
    ctrl = _make_controller(start, end)
    with patch("inverter_control.controller.datetime") as clock:
        for hour in range(24):
            clock.now.return_value = datetime(2026, 10, 29, hour, tzinfo=UTC)
            assert ctrl._in_expensive_window() is (hour in blocked_hours)
    assert not ctrl.victron.method_calls  # No D-Bus/timezone I/O on the control path.


@pytest.mark.parametrize("start,end", [(None, None), (True, 24), (25, 24), (15, "24")])
def test_unknown_or_invalid_settings_block_forced_charge(start, end):
    assert _make_controller(start, end)._in_expensive_window()


def test_unknown_timezone_blocks_enabled_window():
    ctrl = _make_controller()
    ctrl.tou.read.return_value["timezone"] = None
    assert ctrl._in_expensive_window()
    ctrl.tou.read.return_value.update(start=-1, end=-1)
    assert not ctrl._in_expensive_window()


def test_worker_preserves_unknown_gui_settings_instead_of_using_config():
    ctrl = _make_controller()
    ctrl.victron.get_tou_setting.side_effect = lambda path: {
        TOU_START_SETTING: 20,
        TOU_END_SETTING: None,
    }[path]
    ctrl.victron.get_configured_timezone.return_value = UTC
    assert ctrl._read_tou_settings() == {"start": 20, "end": None, "timezone": UTC}


@pytest.mark.parametrize("name", [None, "", "not/a/timezone", "../UTC"])
def test_missing_or_invalid_gx_timezone_is_unknown(name):
    victron = VictronDBus.__new__(VictronDBus)
    victron._dbus_get = MagicMock(return_value=name)
    assert victron.get_configured_timezone() is None


def test_timezone_setting_is_refreshed_instead_of_reusing_legacy_cache():
    victron = VictronDBus.__new__(VictronDBus)
    victron._tz_name = "Europe/Moscow"
    victron._dbus_get = MagicMock(side_effect=["UTC", "America/Los_Angeles"])
    assert str(victron.get_configured_timezone()) == "UTC"
    assert str(victron.get_configured_timezone()) == "America/Los_Angeles"


@pytest.fixture
def rig(monkeypatch):
    from test_main import _make_controller as make_full_controller

    controller, victron, _, _ = make_full_controller()
    controller.calculator = SetpointCalculator({"EMA_ALPHA": 1.0})
    controller.submeter_trim.enabled = False
    controller._grid_ready_for_control = MagicMock(return_value=True)
    controller._skip_changed_grid = MagicMock(return_value=False)
    controller._update_dvcc_limits = MagicMock()
    controller.handle_minimize_charging = MagicMock()
    controller._render_cycle_telemetry = MagicMock()
    controller.tou = MagicMock()
    snapshot = {"start": 15, "end": 24, "timezone": UTC}
    controller.tou.read.side_effect = lambda: dict(snapshot)
    monkeypatch.setattr("inverter_control.controller.ENABLE_GRID_SMOOTHING_WITH_HOME", False)
    clock = MagicMock()
    clock.now.return_value = datetime(2026, 10, 29, 14, tzinfo=UTC)
    monkeypatch.setattr("inverter_control.controller.datetime", clock)
    data = {"g1": 500, "g2": 0, "gt": 500, "t1": 0, "t2": 0, "tt": 0}
    victron.get_system_data.return_value = data
    victron.get_grid_status.return_value = data
    victron.get_mppt_data.return_value = {}
    victron.get_pv_power.return_value = []
    victron.get_inverter_power.return_value = 0
    victron.set_grid_setpoint.return_value = True
    return SimpleNamespace(
        controller=controller, victron=victron, data=data, snapshot=snapshot, clock=clock
    )


def test_desktop_mqtt_charge_switch_is_suppressed_then_resumes(rig):
    import main

    main._handle_toggle(rig.controller, {"entity": "charge_battery"})
    rig.clock.now.return_value = datetime(2026, 10, 29, 15, tzinfo=UTC)
    assert rig.controller.run_cycle()
    assert rig.victron.set_grid_setpoint.call_args.args[0] < 0
    assert "[CHG:TOU]" in rig.controller._render_cycle_telemetry.call_args.args[3]
    assert rig.controller.get_control_flag("charge_battery")
    rig.clock.now.return_value = datetime(2026, 10, 30, 0, tzinfo=UTC)
    assert rig.controller.run_cycle()
    assert rig.victron.set_grid_setpoint.call_args.args[0] > 0
    assert "[CHG]" in rig.controller._render_cycle_telemetry.call_args.args[3]


@pytest.mark.parametrize("change", ["hour", "gui", "snapshot_expired"])
def test_expensive_window_rechecked_at_write_boundary(rig, change):
    rig.controller._control_flags["charge_battery"] = True

    def before_write():
        if change == "hour":
            rig.clock.now.return_value = datetime(2026, 10, 29, 15, tzinfo=UTC)
        elif change == "gui":
            rig.snapshot["start"] = 14
        else:
            rig.snapshot.update(start=None, end=None, timezone=None)
        return rig.data

    rig.victron.get_grid_status.side_effect = before_write
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(0)
    assert rig.controller.previous_setpoint == 0
    assert "[CHG:TOU]" in rig.controller._render_cycle_telemetry.call_args.args[3]


@pytest.mark.parametrize("backup", [False, True])
def test_accepted_charge_does_not_survive_boundary_via_hold_or_slew(rig, backup):
    rig.controller._control_flags["charge_battery"] = True
    if backup:
        rig.data.update(_grid_backup=True, _grid_measurement_time=123)
    assert rig.controller.run_cycle()
    assert rig.victron.set_grid_setpoint.call_args.args[0] > 0
    rig.data.update(gt=0, g1=0)
    rig.clock.now.return_value = datetime(2026, 10, 29, 15, tzinfo=UTC)
    assert rig.controller.run_cycle()
    assert rig.victron.set_grid_setpoint.call_args.args[0] == 0
    flags = rig.controller._render_cycle_telemetry.call_args.args[3]
    assert "[CHG:TOU]" in flags
    if backup:
        assert "[SUBMETER HOLD]" in flags
    assert rig.controller.previous_setpoint == 0


def test_charge_stop_wins_over_conflicting_positive_minimum(rig):
    rig.controller._control_flags["charge_battery"] = True
    rig.controller.set_power_limits(100, 2250)

    def before_write():
        rig.clock.now.return_value = datetime(2026, 10, 29, 15, tzinfo=UTC)
        return rig.data

    rig.victron.get_grid_status.side_effect = before_write
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(0)
    assert "[CHG:limits-conflict]" in rig.controller._render_cycle_telemetry.call_args.args[3]


def test_explicit_numeric_setpoint_is_not_a_charge_switch(rig):
    rig.controller.manual_setpoint = 700
    rig.clock.now.return_value = datetime(2026, 10, 29, 15, tzinfo=UTC)
    assert rig.controller.run_cycle()
    rig.victron.set_grid_setpoint.assert_called_once_with(700)


def test_stalled_settings_reader_never_blocks_calculation(rig):
    entered = threading.Event()
    release = threading.Event()

    def stalled_read():
        entered.set()
        assert release.wait(3)
        return rig.snapshot

    rig.controller.tou = BackgroundReader(
        stalled_read, {"start": None, "end": None, "timezone": None}, name="test-tou"
    )
    rig.controller._control_flags["charge_battery"] = True
    rig.controller.tou.start()
    try:
        assert entered.wait(1)
        assert rig.controller.run_cycle()
        assert rig.victron.set_grid_setpoint.call_args.args[0] < 0
        assert "[CHG:TOU]" in rig.controller._render_cycle_telemetry.call_args.args[3]
    finally:
        release.set()
        assert rig.controller.tou.stop()

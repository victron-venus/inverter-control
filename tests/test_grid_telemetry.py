"""Grid loss, source identity and deterministic control/failsafe regressions."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from inverter_control.dbus_native import NativeDbusClient
from inverter_control.grid_filter import GridFilter
from inverter_control.grid_telemetry import (
    GRID_PHASE_COUNT_PATH,
    GridTelemetry,
    parse_grid_snapshot,
)
from inverter_control.victron import SYSTEM_SERVICE, VictronDBus
from inverter_control.watchdog import HardwareWatchdog

METER = "com.victronenergy.grid.ve_meter"
VEBUS = "com.victronenergy.vebus.ttyUSB0"
L1 = "/Ac/Grid/L1/Power"
L2 = "/Ac/Grid/L2/Power"


def fields(phases=2, source=METER, instance=40, l1=100, l2=-30):
    return {
        GRID_PHASE_COUNT_PATH: phases,
        L1: l1,
        L2: l2,
        "/Ac/In/0/Source": 1,
        "/Ac/In/0/ServiceName": source,
        "/Ac/In/0/DeviceInstance": instance,
    }


def seed(grid, values=None, declared=2):
    grid.replace(fields() if values is None else values)
    meter = grid.selected_meter()
    if meter:
        metadata = {"/Connected": 1}
        if declared is not None:
            metadata["/NrOfPhases"] = declared
        grid.replace_meter(meter, metadata, grid.generation)


def make_victron():
    with (
        patch.object(VictronDBus, "_discover_services"),
        patch.object(VictronDBus, "_load_battery_daily_energy"),
    ):
        v = VictronDBus(test_mode=True)
    v._native = MagicMock()
    v._signal_paths_subscribed = True
    v._shunt_service = "com.victronenergy.battery.shunt"
    seed(v._grid_telemetry)
    assert v.get_grid_status()["_grid_valid"]
    return v


@pytest.mark.parametrize("phases,l2", [(1, None), (2, 0)])
def test_measured_zero_and_legitimate_absent_second_phase(phases, l2):
    grid = GridTelemetry(40)
    seed(grid, fields(phases=phases, l1=0, l2=l2), declared=phases)
    assert grid.snapshot()["_grid_valid"]
    assert grid.snapshot()["gt"] == 0


@pytest.mark.parametrize("raw", [None, [], {}, "nan", "inf", "-inf", "invalid", True])
def test_invalid_power_is_not_stale_or_measured_zero(raw):
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    grid.update(L1, raw)
    assert not grid.snapshot()["_grid_valid"]


@pytest.mark.parametrize("count", [None, 0, 3, 1.5, "nan", True])
def test_unsupported_or_invalid_phase_count(count):
    grid = GridTelemetry(40)
    seed(grid, fields(phases=count))
    assert not grid.snapshot()["_grid_valid"]


def test_availability_derived_phase_count_cannot_hide_lost_l2():
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    seed(grid, fields(phases=1, l2=None), declared=None)
    assert not grid.snapshot()["_grid_valid"]
    assert "decreased" in grid.snapshot()["_grid_invalid_reason"]


def test_declared_meter_topology_rejects_partial_first_observation():
    grid = GridTelemetry(40)
    seed(grid, fields(phases=1, l2=None), declared=2)
    assert not grid.snapshot()["_grid_valid"]
    assert "meter phase topology" in grid.snapshot()["_grid_invalid_reason"]


def test_native_single_phase_and_meters_without_declared_topology_are_supported():
    grid = GridTelemetry(40)
    seed(grid, fields(phases=1, source=VEBUS, l2=None))
    assert grid.snapshot()["_grid_valid"]
    grid = GridTelemetry(40)
    seed(grid, declared=None)
    assert grid.snapshot()["_grid_valid"]


@pytest.mark.parametrize(
    "change", [{"source": VEBUS}, {"source": METER + "_other"}, {"instance": 41}]
)
def test_established_source_cannot_be_replaced_silently(change):
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    seed(grid, fields(**change))
    assert not grid.snapshot()["_grid_valid"]


def test_duplicate_slots_are_allowed_and_ambiguous_identities_rejected():
    grid = GridTelemetry(40)
    values = fields()
    values.update(
        {
            key.replace("/In/0/", "/In/1/"): value
            for key, value in fields().items()
            if "/In/0/" in key
        }
    )
    seed(grid, values)
    assert grid.snapshot()["_grid_valid"]
    values["/Ac/In/1/DeviceInstance"] = 41
    seed(grid, values)
    assert not grid.snapshot()["_grid_valid"]


def test_explicit_startup_contract_rejects_native_fallback_before_meter_seen():
    grid = GridTelemetry(40, expected_service=METER, expected_phases=2)
    seed(grid, fields(phases=1, source=VEBUS, l2=None))
    assert not grid.snapshot()["_grid_valid"]
    seed(grid)
    assert grid.snapshot()["_grid_valid"]


def test_meter_disconnect_invalidates_even_when_system_power_is_numeric():
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    grid.update_meter(METER, "/Connected", 0)
    assert not grid.snapshot()["_grid_valid"]


@pytest.mark.parametrize("service", [SYSTEM_SERVICE, METER])
def test_owner_loss_rejects_inflight_snapshot_and_requires_reseed(service):
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    generation = grid.generation
    assert grid.owner_changed(service)
    assert not grid.replace(fields(), generation)
    assert not grid.snapshot()["_grid_valid"]
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    assert grid.snapshot()["_grid_source"] == METER


def test_explicit_invalid_and_metadata_changes_reject_old_snapshot():
    grid = GridTelemetry(40)
    seed(grid)
    generation = grid.generation
    grid.update(L2, None)
    assert not grid.replace(fields(), generation)
    generation = grid.generation
    grid.update(GRID_PHASE_COUNT_PATH, 1)
    assert not grid.replace(fields(), generation)


def test_unchanged_values_are_revalidated_without_any_change_signal(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    grid = GridTelemetry(40)
    seed(grid)
    for now[0] in (130.0, 160.0, 190.0):
        assert grid.snapshot()["_grid_valid"]
        seed(grid)
    now[0] = 230.1
    assert not grid.snapshot()["_grid_valid"]


def test_shunt_and_unrelated_grid_traffic_cannot_refresh_required_phase(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    v = make_victron()
    now[0] = 141.0
    v._apply_fast_value(v._shunt_service, "/Dc/0/Power", "1200")
    v._apply_fast_value(SYSTEM_SERVICE, L1, "200")
    assert not v.get_grid_status()["_grid_valid"]
    assert v.get_ac_in_power() is None


def test_native_invalid_array_and_text_only_change_are_distinct():
    v = make_victron()
    client = NativeDbusClient.__new__(NativeDbusClient)
    client._handlers_lock = threading.Lock()
    client._signal_handlers = [v._on_fast_signal]
    client._dispatch(L1, {"Text": SimpleNamespace(value="0 W")}, SYSTEM_SERVICE)
    assert v.get_grid_status()["_grid_valid"]
    client._dispatch(L1, {"Value": SimpleNamespace(value=[])}, SYSTEM_SERVICE)
    assert not v.get_grid_status()["_grid_valid"]


def test_native_root_snapshot_preserves_invalid_and_zero():
    client = NativeDbusClient.__new__(NativeDbusClient)
    client.call_busitem = MagicMock(
        return_value=SimpleNamespace(
            body=[
                {
                    "Ac/Grid/L1/Power": SimpleNamespace(value=0),
                    "Ac/Grid/L2/Power": SimpleNamespace(value=[]),
                }
            ]
        )
    )
    assert client.get_values(SYSTEM_SERVICE) == {L1: "0", L2: None}
    client.call_busitem.assert_called_once_with(SYSTEM_SERVICE, "/", "GetValue", timeout=0.5)


@pytest.mark.parametrize("reply", [None, SimpleNamespace(body=[]), SimpleNamespace(body=["bad"])])
def test_native_root_snapshot_rejects_unusable_reply(reply):
    client = NativeDbusClient.__new__(NativeDbusClient)
    client.call_busitem = MagicMock(return_value=reply)
    assert client.get_values(SYSTEM_SERVICE) is None


def test_tree_parser_keeps_zero_distinct_from_invalid_and_missing():
    output = 'string "Ac/Grid/L1/Power"\nvariant double 0\nstring "Ac/Grid/L2/Power"\nvariant array [\n]\n'
    parsed = parse_grid_snapshot(output)
    assert parsed[L1] == "0"
    assert parsed[L2] is None
    assert parsed[GRID_PHASE_COUNT_PATH] is None


def test_seed_reply_from_before_owner_loss_does_not_revive_control():
    v = make_victron()

    def old_reply(_service):
        v._on_name_owner_changed(SYSTEM_SERVICE, ":1.1", "")
        return fields()

    v._native.get_values.side_effect = old_reply
    v._native.get_value.return_value = None
    v._seed_fast_values()
    assert not v.get_grid_status()["_grid_valid"]


def test_meter_read_from_before_owner_loss_does_not_revive_control():
    v = make_victron()
    generation = v._grid_telemetry.generation

    def stale_meter_reply(_service):
        v._on_name_owner_changed(METER, ":1.1", "")
        return {"/Connected": "1", "/NrOfPhases": "2"}

    v._native.get_values.side_effect = stale_meter_reply
    v._refresh_grid_meter(generation)
    assert not v.get_grid_status()["_grid_valid"]


def test_filter_reset_rejects_sample_acquired_before_invalidation():
    holder = {}

    def getter():
        holder["filter"].reset()
        return 900.0

    grid_filter = GridFilter(getter=getter)
    holder["filter"] = grid_filter
    grid_filter.stop_event = MagicMock()
    grid_filter.stop_event.wait.side_effect = [False, True]
    grid_filter.run()
    assert grid_filter.value() is None


@pytest.fixture(name="control")
def _control_fixture():
    from test_main import _make_controller

    with patch("inverter_control.controller.get_webhook_server"):
        controller, victron, _, _ = _make_controller()
    controller._update_dvcc_limits = MagicMock()
    controller.calculate_setpoint = MagicMock(return_value=(-400, ""))
    controller.handle_minimize_charging = MagicMock()
    controller.update_state = MagicMock()
    controller.get_boolean = MagicMock(return_value=False)
    controller._watchdog = HardwareWatchdog(victron, timeout_seconds=30, get_setpoint=lambda: -500)
    controller._watchdog._last_dbus_update = 100.0
    controller._watchdog._last_setpoint_update = 100.0
    victron.get_system_data.return_value = {"_grid_valid": True, "gt": 100}
    victron.get_grid_status.return_value = {"_grid_valid": True}
    victron.set_grid_setpoint.return_value = True
    with patch("inverter_control.controller.broadcast_line"):
        yield controller, victron


def test_missing_validity_contract_blocks_control_even_with_numeric_grid(control):
    controller, victron = control
    victron.get_system_data.return_value = {"gt": 0}
    assert controller.run_cycle()
    victron.set_grid_setpoint.assert_not_called()
    assert controller._watchdog._last_dbus_update == 100.0


def test_outage_timing_manual_preservation_and_orderly_recovery(control, monkeypatch):
    controller, victron = control
    now = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    controller.manual_setpoint = -700
    victron.get_system_data.return_value = {"_grid_valid": False}
    for now[0] in (105.0, 130.0, 135.0, 140.0):
        controller.run_cycle()
        controller._watchdog._check_heartbeat()
        victron.set_grid_setpoint.assert_not_called()
    now[0] = 145.0
    controller.run_cycle()
    controller._watchdog._check_heartbeat()
    victron.set_grid_setpoint.assert_called_once_with(0)
    assert controller.manual_setpoint == -700
    assert controller._watchdog._last_dbus_update == 100.0
    assert controller._watchdog._last_setpoint_update == 100.0
    victron.get_system_data.return_value = {"_grid_valid": True, "gt": 0}
    for now[0] in (150.0, 155.0):
        controller.run_cycle()
        controller._watchdog._check_heartbeat()
    assert [call.args[0] for call in victron.set_grid_setpoint.call_args_list] == [0, -500]
    assert controller.manual_setpoint == -700
    now[0] = 156.0
    controller.run_cycle()
    assert [call.args[0] for call in victron.set_grid_setpoint.call_args_list] == [0, -500, -700]
    assert controller.manual_setpoint is None
    victron.set_ess_mode.assert_not_called()


def test_brief_recovery_then_invalid_does_not_restore_stale_setpoint(control, monkeypatch):
    controller, victron = control
    watchdog = controller._watchdog
    now = [145.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    watchdog.mark_dbus_invalid()
    for _ in range(3):
        watchdog._check_heartbeat()
    now[0] = 150.0
    watchdog.mark_dbus_update()
    watchdog._check_heartbeat()
    watchdog.mark_dbus_invalid()
    for now[0] in (155.0, 160.0):
        watchdog._check_heartbeat()
    assert watchdog.is_triggered()
    victron.set_grid_setpoint.assert_called_once_with(0)


def test_rejected_failsafe_and_restore_are_retried_before_normal_control(control, monkeypatch):
    controller, victron = control
    watchdog = controller._watchdog
    now = [145.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    victron.set_grid_setpoint.side_effect = [False, True, False, True]
    watchdog.mark_dbus_invalid()
    for _ in range(4):
        watchdog._check_heartbeat()
    assert watchdog._hardware_forced
    now[0] = 150.0
    watchdog.mark_dbus_update()
    watchdog._check_heartbeat()
    watchdog._check_heartbeat()
    assert watchdog.is_triggered()
    controller.run_cycle()
    assert victron.set_grid_setpoint.call_count == 3
    watchdog._check_heartbeat()
    assert not watchdog.is_triggered()
    assert [call.args[0] for call in victron.set_grid_setpoint.call_args_list] == [0, 0, -500, -500]


def test_rejected_normal_write_does_not_feed_watchdog_or_consume_manual(control):
    controller, victron = control
    controller.manual_setpoint = -700
    victron.set_grid_setpoint.return_value = False
    controller.run_cycle()
    assert controller.manual_setpoint == -700
    assert controller.previous_setpoint == 0
    assert controller._watchdog._last_setpoint_update == 100.0


def test_invalidation_during_calculation_prevents_normal_write(control):
    controller, victron = control

    def invalidate(_snapshot):
        victron.get_grid_status.return_value = {
            "_grid_valid": False,
            "_grid_invalid_reason": "owner lost",
        }
        return -400, ""

    controller.calculate_setpoint.side_effect = invalidate
    controller.run_cycle()
    victron.set_grid_setpoint.assert_not_called()
    assert controller._watchdog._telemetry_invalid


def test_partial_metadata_read_never_admits_a_partial_first_observation():
    grid = GridTelemetry(40)
    grid.replace(fields(phases=1, l2=None))
    grid.update_meter(METER, "/Connected", 1)
    assert not grid.snapshot()["_grid_valid"]
    grid.replace_meter(METER, {"/Connected": 1, "/NrOfPhases": 2}, grid.generation)
    assert not grid.snapshot()["_grid_valid"]


def test_declared_phase_capability_cannot_disappear_after_observation():
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    grid.replace_meter(METER, {"/Connected": 1}, grid.generation)
    assert not grid.snapshot()["_grid_valid"]


def test_meter_timeout_is_distinct_from_optional_missing_phase_capability():
    grid = GridTelemetry(40)
    seed(grid, declared=None)
    assert grid.snapshot()["_grid_valid"]
    grid.replace_meter(METER, None, grid.generation)
    assert not grid.snapshot()["_grid_valid"]


def test_newer_invalid_root_reply_prevents_older_valid_reply_from_reviving_grid():
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    generation = grid.generation
    grid.replace(fields(l2=None), generation)
    assert not grid.snapshot()["_grid_valid"]
    assert grid.replace(fields(), generation) is None
    assert not grid.snapshot()["_grid_valid"]


def test_newer_disconnected_reply_prevents_older_connected_reply_from_reviving_grid():
    grid = GridTelemetry(40)
    seed(grid)
    assert grid.snapshot()["_grid_valid"]
    generation = grid.generation
    assert grid.replace_meter(METER, {"/Connected": 0, "/NrOfPhases": 2}, generation)
    assert not grid.snapshot()["_grid_valid"]
    assert not grid.replace_meter(METER, {"/Connected": 1, "/NrOfPhases": 2}, generation)
    assert not grid.snapshot()["_grid_valid"]


def test_filter_skips_unavailable_and_nonfinite_without_debug_flood(caplog):
    readings = iter([None, None, float("nan"), float("inf"), 12.0])
    grid_filter = GridFilter(getter=lambda: next(readings))
    grid_filter.stop_event = MagicMock()
    grid_filter.stop_event.wait.side_effect = [False] * 5 + [True]
    with caplog.at_level("DEBUG"):
        grid_filter.run()
    assert grid_filter.value() == 12.0
    assert grid_filter._errors == 2
    assert not caplog.records


def test_synchronous_owner_callback_is_safe_during_constructor():
    native = MagicMock()
    native.add_name_owner_handler.side_effect = lambda callback: callback(
        SYSTEM_SERVICE, ":1.1", ""
    )
    with (
        patch("inverter_control.victron.USE_NATIVE_DBUS", True),
        patch("inverter_control.victron.NativeDbusClient", return_value=native),
        patch.object(VictronDBus, "_start_background_polling"),
        patch.object(VictronDBus, "_discover_services"),
        patch.object(VictronDBus, "_load_battery_daily_energy"),
    ):
        v = VictronDBus()
    assert v._discovery_requested.is_set()
    assert not v.get_grid_status()["_grid_valid"]
    assert "owner changed" in v.get_grid_status()["_grid_invalid_reason"]


@pytest.mark.parametrize(
    "setting,value",
    [
        ("GRID_EXPECTED_PHASES", 3),
        ("GRID_EXPECTED_PHASES", True),
        ("GRID_EXPECTED_PHASES", "2"),
        ("GRID_EXPECTED_SERVICE", None),
        ("GRID_EXPECTED_SERVICE", ":1.40"),
    ],
)
def test_invalid_startup_expectations_are_rejected(setting, value, monkeypatch):
    from inverter_control import config

    monkeypatch.setattr(config, setting, value)
    with pytest.raises(ValueError, match=setting):
        config._validate_config()


def _tree(values):
    lines = []
    for path, value in values.items():
        lines.append(f'string "{path.lstrip("/")}"')
        if value is None:
            lines.append("variant array [\n]")
        elif isinstance(value, str):
            lines.append(f'variant string "{value}"')
        else:
            lines.append(f"variant double {value}")
    return "\n".join(lines) + "\n"


def test_cli_grid_and_meter_fallback_revalidate_invalidation_and_recovery():
    v = make_victron()
    v._native.get_values.return_value = None
    meter_tree = _tree({"/Connected": 1, "/NrOfPhases": 2})
    for values, expected_valid in ((fields(), True), (fields(l2=None), False), (fields(), True)):
        with patch.object(
            v, "_safe_subprocess_tracked", side_effect=[_tree(values), meter_tree]
        ) as query:
            v._poll_system_data()
        assert v.get_grid_status()["_grid_valid"] is expected_valid
        assert query.call_count == 2
    assert v.get_ac_in_power() == 70


def test_successful_root_seed_keeps_control_getter_in_cache():
    v = make_victron()
    v._native.get_values.side_effect = lambda service: (
        fields() if service == SYSTEM_SERVICE else {"/Connected": 1, "/NrOfPhases": 2}
    )
    v._native.get_value.return_value = None
    v._seed_fast_values()
    with patch.object(v, "_safe_subprocess") as synchronous_query:
        data = v.get_system_data()
    synchronous_query.assert_not_called()
    assert data["_grid_valid"]
    assert data["gt"] == 70


def test_old_cli_reply_cannot_revive_grid_after_owner_loss():
    v = make_victron()

    def reply(*_args, **_kwargs):
        v._on_name_owner_changed(SYSTEM_SERVICE, ":1.1", "")
        return _tree(fields())

    with patch.object(v, "_safe_subprocess_tracked", side_effect=reply) as query:
        v._poll_system_data()
    assert query.call_count == 1
    assert not v.get_grid_status()["_grid_valid"]

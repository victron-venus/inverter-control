"""Submeter identity, freshness and failover are independent of the primary meter."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_main import _make_controller

from inverter_control.dbus_native import NativeDbusClient
from inverter_control.grid_backup import GridBackup, parse_backup_snapshot

SERVICE = "com.victronenergy.acload.backup"
PRIMARY = {"_grid_valid": True, "_grid_source": "primary", "g1": 4, "g2": 3, "gt": 7}
LOST = {**PRIMARY, "_grid_valid": False, "_grid_invalid_reason": "offline"}


@pytest.fixture
def meter_clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inverter_control.grid_backup.time.monotonic", lambda: now[0])
    monkeypatch.setattr("inverter_control.grid_backup.time.time", lambda: 1000 + now[0])
    return now


def snapshot(**changes):
    fields = {
        "/Connected": 1,
        "/Role": "acload",
        "/Ac/Power": -87.25,
        "/LastUpdate": 1100,
        "/DeviceInstance": 78,
        "/CustomName": "Main supply",
        "/ProductName": "Meter",
    }
    return {**fields, **changes}


def ready_backup(*, enabled=True):
    backup = GridBackup(SERVICE, 30, 5, enabled=enabled)
    backup.replace(snapshot(), backup.generation)
    return backup


def test_disabled_fallback_still_exposes_detected_submeter(meter_clock):
    backup = ready_backup(enabled=False)
    selected = backup.select(LOST)
    assert not selected["_grid_valid"]
    assert not selected["_grid_backup"]
    assert selected["_grid_backup_status"] == {
        "enabled": False,
        "available": True,
        "service": SERVICE,
        "device_instance": 78,
        "name": "Main supply",
        "power": -87.25,
        "measurement_time": 1100,
        "age_seconds": 0,
    }


def test_primary_priority_and_return_hysteresis(meter_clock):
    backup = ready_backup()
    assert backup.select(PRIMARY)["gt"] == 7
    selected = backup.select(LOST)
    assert selected["_grid_backup"] and selected["_grid_valid"]
    assert (selected["gt"], selected["g1"], selected["g2"]) == (-87, None, None)
    assert selected["_grid_measurement_time"] == 1100
    generation = selected["_grid_selection_generation"]
    assert backup.select(PRIMARY)["_grid_backup"]
    for meter_clock[0] in (102, 104, 105):
        backup.replace(snapshot(), backup.generation)
        selected = backup.select(PRIMARY)
    assert selected["_grid_source"] == "primary"
    assert not selected["_grid_backup"]
    assert selected["_grid_selection_generation"] == generation + 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("/Connected", 0),
        ("/Role", "grid"),
        ("/Ac/Power", None),
        ("/Ac/Power", float("nan")),
        ("/Ac/Power", float("inf")),
        ("/DeviceInstance", -1),
        ("/DeviceInstance", 1.5),
        ("/LastUpdate", 1069),
        ("/LastUpdate", 1106),
        ("/LastUpdate", None),
    ],
)
def test_bad_sample_never_controls(meter_clock, field, value):
    backup = GridBackup(SERVICE, 30, 5, enabled=True)
    backup.replace(snapshot(**{field: value}), backup.generation)
    selected = backup.select(LOST)
    assert not selected["_grid_valid"]
    assert not selected["_grid_backup_available"]
    assert selected["_grid_backup_status"]["power"] is None


def test_read_freshness_and_source_age_both_expire(meter_clock):
    backup = ready_backup()
    meter_clock[0] = 103.1
    assert not backup.select(LOST)["_grid_backup_available"]
    backup.replace(snapshot(), backup.generation)
    assert backup.select(LOST)["_grid_backup_available"]
    meter_clock[0] = 130
    backup.replace(snapshot(), backup.generation)
    assert not backup.select(LOST)["_grid_backup_available"]


def test_disconnect_retains_identity_but_rejects_inflight_old_reply(meter_clock):
    backup = ready_backup()
    generation = backup.generation
    backup.invalidate()
    backup.replace(snapshot(), generation)
    status = backup.select(LOST)["_grid_backup_status"]
    assert status["name"] == "Main supply" and status["device_instance"] == 78
    assert status["power"] is None and not status["available"]
    backup.replace(snapshot(**{"/CustomName": ""}), backup.generation)
    assert backup.select(LOST)["_grid_backup_status"]["name"] == "Meter"


def test_undiscovered_service_has_no_identity(meter_clock):
    backup = GridBackup(SERVICE, 30, 5)
    status = backup.select(PRIMARY)["_grid_backup_status"]
    assert status["name"] is None and status["device_instance"] is None


def test_cli_and_native_snapshot_preserve_text_and_signed_power():
    output = """string "/Role" array [ dict entry(string "Value" variant string "acload") ]
string "/Ac/Power" array [ dict entry(string "Value" variant double -123.5 ) ]
string "/CustomName" array [ dict entry(string "Value" variant string "Main supply") ]"""
    parsed = parse_backup_snapshot(output)
    assert parsed["/Role"] == "acload" and parsed["/CustomName"] == "Main supply"
    assert float(parsed["/Ac/Power"]) == -123.5
    client = NativeDbusClient()
    client.call_busitem = MagicMock(
        return_value=SimpleNamespace(
            body=[
                {
                    path: {"Value": SimpleNamespace(value=value)}
                    for path, value in snapshot().items()
                }
            ]
        )
    )
    assert client.get_items_values(SERVICE) == snapshot()
    client.call_busitem.assert_called_once_with(SERVICE, "/", "GetItems", timeout=0.5)


@pytest.mark.parametrize(
    "timestamp,expected",
    [
        ("1.78935e+09", None),
        ("nan", None),
        ("invalid", None),
        ("1789350000", "1789350000"),
        ("1.789350000e+09", "1.789350000e+09"),
        ("1789350000.123", "1789350000.123"),
    ],
)
def test_cli_rejects_timestamp_rounding_that_can_conceal_staleness(timestamp, expected):
    output = (
        f'string "/LastUpdate" array [ dict entry(string "Value" variant double {timestamp} ) ]'
    )
    assert parse_backup_snapshot(output)["/LastUpdate"] == expected


def test_backup_state_survives_slim_mqtt_and_unchanged_samples_hold(meter_clock, monkeypatch):
    controller, _, _, calculator = _make_controller()
    selected = ready_backup().select(PRIMARY)
    controller._grid_ready_for_control(selected)
    monkeypatch.setattr("inverter_control.controller.MQTT_SLIM_STATE", True)
    payload = controller.get_state_for_mqtt()
    assert payload["grid_backup"]["name"] == "Main supply"
    assert payload["grid_backup"]["power"] == -87.25
    assert not payload["grid_using_backup"]
    controller._last_backup_measurement = 1100
    controller.previous_setpoint = -200
    assert controller.calculate_setpoint(
        {"_grid_backup": True, "_grid_measurement_time": 1100}
    ) == (
        -200,
        "[SUBMETER HOLD] ",
    )
    calculator.calculate.assert_not_called()


@pytest.mark.parametrize("selection", ["available", "in_use", "unavailable", "cleared"])
def test_state_rebuild_publishes_current_submeter_selection(meter_clock, monkeypatch, selection):
    controller, victron, _, _ = _make_controller()
    victron.get_battery_chain_socs.return_value = []
    victron.get_inverter_state.return_value = (9, "Inverting")
    victron.get_ess_mode.return_value = {"is_external": True}
    victron.get_acload_powers.return_value = {}
    victron.get_battery_soc_local.return_value = 50
    for method, value in (
        ("_get_cached_batteries", []),
        ("_get_cached_mppt_chargers", []),
        ("_get_ev_state", {}),
        ("_get_water_state", {}),
        ("_get_ha_status", {}),
        ("_get_daily_stats", {}),
    ):
        monkeypatch.setattr(controller, method, MagicMock(return_value=value))
    controller._last_perf_snapshot = time.time()
    monkeypatch.setattr("inverter_control.controller.MQTT_SLIM_STATE", True)
    controller._control_flags.update(house_support=True, no_feed=True)
    flags = dict(controller._control_flags)

    backup = ready_backup()
    controller._grid_ready_for_control(backup.select(PRIMARY))
    assert controller.get_state_for_mqtt()["grid_backup"]["power"] == -87.25

    if selection == "cleared":
        current = dict(PRIMARY)
        expected_backup = None
    else:
        connected = selection in ("available", "in_use")
        backup.replace(
            snapshot(**{"/Connected": int(connected), "/Ac/Power": 0, "/LastUpdate": 1101}),
            backup.generation,
        )
        current = backup.select(LOST if selection == "in_use" else PRIMARY)
        expected_backup = {
            "enabled": True,
            "available": connected,
            "service": SERVICE,
            "device_instance": 78,
            "name": "Main supply",
            "power": 0 if connected else None,
            "measurement_time": 1101,
            "age_seconds": 0,
        }
    controller.update_state(current, -200)
    payload = controller.get_state_for_mqtt()

    assert payload["grid_backup"] == expected_backup
    assert payload["grid_backup_available"] is (selection in ("available", "in_use"))
    assert payload["grid_using_backup"] is (selection == "in_use")
    assert payload["grid_control_source"] == (SERVICE if selection == "in_use" else "primary")
    assert payload["grid_control_power"] == (0 if selection == "in_use" else 7)
    assert payload["grid_primary_reason"] == ("offline" if selection == "in_use" else None)
    assert payload["booleans"] == flags


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_applies_flag_but_polls_selected_service_for_display(
    meter_clock, monkeypatch, enabled
):
    from inverter_control import victron

    monkeypatch.setattr(victron, "GRID_BACKUP_SERVICE", SERVICE)
    monkeypatch.setattr(victron, "USE_GRID_SUBMETER_AS_BACKUP", enabled)
    monkeypatch.setattr(victron.VictronDBus, "_discover_services", lambda _: None)
    reader = victron.VictronDBus(test_mode=True)
    reader._native = SimpleNamespace(get_items_values=MagicMock(return_value=snapshot()))
    reader._poll_grid_backup()
    reader._poll_grid_backup()
    reader._native.get_items_values.assert_called_once_with(SERVICE, timeout=0.5)
    status = reader.get_grid_status()
    assert status["_grid_backup"] is enabled
    assert status["_grid_backup_status"]["name"] == "Main supply"

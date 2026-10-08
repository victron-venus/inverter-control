"""Battery telemetry absence is neither zero charge nor permission to actuate."""

import time
from unittest.mock import Mock

import pytest

from inverter_control.console_ui import ConsoleUI
from inverter_control.victron import (
    BATTERY_CHAIN_1,
    SHUNT_READ_MAX_AGE,
    SHUNT_SIGNAL_PATHS,
    VictronDBus,
)
from inverter_control.victron_parse import calculate_battery_soc_from_voltage


@pytest.fixture
def device(monkeypatch):
    monkeypatch.setattr(VictronDBus, "_discover_services", lambda self: None)
    value = VictronDBus(test_mode=True)
    value._shunt_service = "com.victronenergy.battery.shunt"
    value._system_data = {"_last_update": time.time(), "bv": 52, "bc": 0, "bp": 0}
    monkeypatch.setattr(value, "_safe_subprocess", Mock(return_value=None))
    return value


@pytest.mark.parametrize("voltage", [None, 0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_voltage_is_unknown_not_empty_battery(voltage):
    assert calculate_battery_soc_from_voltage(voltage) is None


def test_valid_empty_battery_and_zero_current_remain_real_numbers(device):
    device._system_data["bv"] = 40
    data = device.get_system_data()
    assert data["battery_data"]["available"]
    assert data["bc"] == data["bp"] == 0
    assert device.get_battery_soc_local(data) == 0


def test_missing_shunt_cannot_reuse_previous_identity_values(device):
    device._shunt_service = None
    data = device.get_system_data()
    assert [data[key] for key in ("bv", "bc", "bp")] == [None] * 3
    assert data["battery_data"]["reason"] == "missing_source"
    assert device.get_battery_soc_local(data) is None


@pytest.mark.parametrize("raw", [None, "nan", "0", "bad"])
def test_invalid_voltage_signal_immediately_clears_last_good(device, raw):
    device._apply_fast_value(device._shunt_service, "/Dc/0/Voltage", raw)
    data = device.get_system_data()
    assert data["bv"] is None
    assert device.get_battery_soc_local(data) is None


def test_disconnect_signal_clears_all_values(device):
    device._apply_fast_value(device._shunt_service, "/Connected", "0")
    data = device.get_system_data()
    assert [data[key] for key in ("bv", "bc", "bp")] == [None] * 3
    assert data["battery_data"]["reason"] == "disconnected"


def test_disconnected_signals_cannot_supply_readings_after_reconnect(device):
    """Only readings observed after reconnection can restore availability."""
    service = device._shunt_service
    device._apply_fast_value(service, "/Connected", "0")
    for path in SHUNT_SIGNAL_PATHS:
        device._apply_fast_value(service, path, "52")
    device._apply_fast_value(service, "/Connected", "1")
    assert not device.get_system_data()["battery_data"]["available"]
    assert device._shunt_read_times == {}
    for path, value in zip(SHUNT_SIGNAL_PATHS, ("52", "0", "0"), strict=True):
        device._apply_fast_value(service, path, value)
    assert device.get_system_data()["battery_data"]["available"]


@pytest.mark.parametrize("transport", ["native", "cli", "synchronous"])
@pytest.mark.parametrize("newer", ["disconnect", "owner", "measurement"])
def test_inflight_shunt_reply_cannot_overwrite_newer_observation(
    device, monkeypatch, transport, newer
):
    """Interleave a newer event after read dispatch and before its old reply."""
    from tests.test_native_reconciliation import tree

    service = device._shunt_service
    fields = {"/Connected": "1", "/Dc/0/Voltage": "54", "/Dc/0/Current": "2", "/Dc/0/Power": "108"}

    def newer_event():
        if newer == "disconnect":
            device._apply_fast_value(service, "/Connected", "0")
        elif newer == "owner":
            device._on_name_owner_changed(service, ":1.1", ":1.2")
        else:
            device._apply_fast_value(service, "/Dc/0/Power", "0")

    if transport == "native":

        def native_reply(_service):
            newer_event()
            return fields, None

        monkeypatch.setattr(device, "_native_reconciliation_read", native_reply)
        device._poll_shunt_data()
    else:

        def old_reply(*_args, **_kwargs):
            newer_event()
            return tree(fields)

        if transport == "cli":
            monkeypatch.setattr(device, "_native_reconciliation_read", lambda _: (None, None))
            monkeypatch.setattr(device, "_reconciliation_fallback", old_reply)
            device._poll_shunt_data()
        else:
            device._system_data["_last_update"] = 0
            monkeypatch.setattr(device, "_refresh_grid_meter", lambda _: None)
            monkeypatch.setattr(
                device,
                "_safe_subprocess",
                lambda args, **kwargs: (
                    old_reply() if f"--dest={service}" in args else "system tree"
                ),
            )
            data = device.get_system_data()
            assert data["bp"] == (0 if newer == "measurement" else None)
    data = device.get_system_data()
    assert data["bp"] == (0 if newer == "measurement" else None)
    if newer != "measurement":
        assert not data["battery_data"]["available"]


def test_old_display_copy_cannot_pair_with_new_read_timestamps(device):
    """Display values and quality metadata must describe the same observation."""
    old = dict(device._system_data)
    device._apply_fast_value(device._shunt_service, "/Dc/0/Power", "123")
    device._merge_battery_status(old)
    assert old["bp"] == 123


def test_seed_reply_cannot_undo_disconnect_received_during_seed_read(device, monkeypatch):
    """A reconnect seed is subject to the same ordering guard as tree polls."""
    service = device._shunt_service
    device._native = Mock()
    device._native.get_values.return_value = None
    monkeypatch.setattr(device, "_fast_targets", lambda: [(service, "/Connected")])

    def old_connected_reply(*_args):
        device._apply_fast_value(service, "/Connected", "0")
        return "1"

    device._native.get_value.side_effect = old_connected_reply
    device._seed_fast_values()
    assert device._shunt_connected is False
    assert not device.get_system_data()["battery_data"]["available"]


def test_failed_poll_cannot_clear_a_newer_valid_sample(device, monkeypatch):
    """A failed old request cannot erase telemetry received while it waited."""
    monkeypatch.setattr(device, "_native_reconciliation_read", lambda _: (None, None))

    def late_failure(*_args, **_kwargs):
        device._apply_fast_value(device._shunt_service, "/Dc/0/Power", "123")

    monkeypatch.setattr(device, "_reconciliation_fallback", late_failure)
    device._poll_shunt_data()
    assert device.get_system_data()["bp"] == 123


@pytest.mark.parametrize("missing_source", [False, True])
def test_missing_shunt_reply_does_not_create_read_timestamps(device, monkeypatch, missing_source):
    """No source or no response is an invalidation, never a fresh observation."""
    if missing_source:
        device._shunt_service = None
    monkeypatch.setattr(device, "_native_reconciliation_read", lambda _: (None, None))
    monkeypatch.setattr(device, "_reconciliation_fallback", lambda *a, **kw: None)
    previous_update = device._system_data["_last_update"]
    device._poll_shunt_data()
    assert device._shunt_read_times == {}
    assert device._system_data["_last_update"] == previous_update
    assert device._system_data["bp"] is None


def test_local_read_age_expires_without_claiming_physical_sample_age(device, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inverter_control.victron.time.monotonic", lambda: now[0])
    device._accept_shunt_data({"bv": 52, "bc": 0, "bp": 0})
    data = device.get_system_data()
    assert data["battery_data"]["read_age_seconds"] == 0
    assert data["battery_data"]["sample_age_seconds"] is None
    now[0] += SHUNT_READ_MAX_AGE + 1
    data = device.get_system_data()
    assert data["battery_data"]["reason"] == "stale_read"
    assert [data[key] for key in ("bv", "bc", "bp")] == [None] * 3


def test_battery_source_frozen_timestamp_expires_and_recovers(device, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inverter_control.victron.time.monotonic", lambda: now[0])
    monkeypatch.setattr(device, "_dbus_get", lambda *_: "1")
    metadata = {
        "/Info/DataComplete": "1",
        "/Info/LastMeasurementMonotonic": "100",
        "/Info/DataTimeout": "60",
    }
    monkeypatch.setattr(device, "_dbus_get_native_only", lambda _, path: metadata.get(path))
    assert device._battery_source_status(BATTERY_CHAIN_1)["available"]
    now[0] = 160
    assert device._battery_source_status(BATTERY_CHAIN_1)["reason"] == "stale_sample"
    metadata["/Info/LastMeasurementMonotonic"] = "160"
    assert device._battery_source_status(BATTERY_CHAIN_1)["available"]
    metadata["/Info/DataComplete"] = "0"
    assert device._battery_source_status(BATTERY_CHAIN_1)["reason"] == "incomplete"


@pytest.mark.parametrize("metadata_kind", ["timestamp", "complete"])
def test_observed_metadata_contract_cannot_disappear_into_legacy_fallback(
    device, monkeypatch, metadata_kind
):
    """A successful CLI read cannot erase a previously observed native contract."""
    monkeypatch.setattr("inverter_control.victron.time.monotonic", lambda: 100.0)
    metadata = (
        {"/Info/LastMeasurementMonotonic": "100", "/Info/DataTimeout": "60"}
        if metadata_kind == "timestamp"
        else {"/Info/DataComplete": "1"}
    )
    saved = dict(metadata)
    monkeypatch.setattr(device, "_dbus_get_native_only", lambda _, path: metadata.get(path))
    monkeypatch.setattr(device, "_dbus_get", lambda _, path: "1" if path == "/Connected" else "55")
    assert device._battery_source_status(BATTERY_CHAIN_1)["available"]
    metadata.clear()
    assert device._battery_source_status(BATTERY_CHAIN_1)["reason"] == "incomplete"
    assert device._query_battery_chain_socs()[0] is None
    metadata.update(saved)
    assert device._query_battery_chain_socs()[0] == 55


def test_legacy_source_without_metadata_remains_supported(device, monkeypatch):
    """Do not require new producer metadata from a genuine legacy source."""
    monkeypatch.setattr(device, "_dbus_get_native_only", lambda *_: None)
    monkeypatch.setattr(device, "_dbus_get", lambda *_: "1")
    assert device._battery_source_status(BATTERY_CHAIN_1)["available"]


@pytest.mark.parametrize("raw", [None, "bad", "nan", "inf"])
def test_invalid_time_to_go_remains_unknown(device, monkeypatch, raw):
    monkeypatch.setattr(device, "_dbus_get", lambda *_: "1")
    monkeypatch.setattr(
        device, "_dbus_get_native_only", lambda _, path: raw if path == "/TimeToGo" else None
    )
    device._reconcile_all_batteries()
    for battery in device.get_all_batteries():
        assert battery["available"]
        assert battery["time_to_go_sec"] is None
        assert battery["time_to_go"] == ""


def test_virtual_readiness_without_timestamp_and_real_zero_soc(device, monkeypatch):
    monkeypatch.setattr(
        device,
        "_dbus_get_native_only",
        lambda _, path: "1" if path == "/Info/DataComplete" else None,
    )
    monkeypatch.setattr(device, "_dbus_get", lambda _, path: "1" if path == "/Connected" else "0")
    assert device._query_battery_chain_socs() == [0.0, 0.0]
    monkeypatch.setattr(device, "_dbus_get", lambda *_: None)
    assert device._query_battery_chain_socs() == [None, None]
    device._reconcile_all_batteries()
    for battery in device.get_all_batteries():
        assert not battery["available"]
        assert all(battery[key] is None for key in ("voltage", "current", "power", "soc"))


def test_console_handles_unknown_and_real_zero_separately(device):
    device.get_inverter_state = Mock(return_value=(9, "Inverting"))
    ui = ConsoleUI(Mock(), device)
    unknown = ui._fmt_battery_section(
        {"bv": None, "bc": None, "bp": None, "battery_socs": [None, 0]}
    )
    assert "—W" in unknown and "—%,0%" in unknown
    zero = ui._fmt_battery_section({"bv": 40, "bc": 0, "bp": 0, "battery_socs": [0, 0]})
    assert "0W,0%,0%,0%" in zero

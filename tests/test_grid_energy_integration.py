"""Existing meter snapshots feed display energy without extra reads or control I/O."""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from inverter_control.grid_energy import GridEnergyLedger
from inverter_control.victron import SETTINGS_SERVICE, SYSTEM_SERVICE, TIME_ZONE_PATH, VictronDBus
from tests.test_grid_energy import METER, ZONE, Clock, readings
from tests.test_grid_energy_subscriptions import client, reject_timezone


def system_fields(meter=METER, instance=40):
    return {
        "/Ac/Grid/NumberOfPhases": 2,
        "/Ac/Grid/L1/Power": 100,
        "/Ac/Grid/L2/Power": 100,
        "/Ac/In/0/Source": 1,
        "/Ac/In/0/ServiceName": meter,
        "/Ac/In/0/DeviceInstance": instance,
        # Inverter AC-input disconnection does not disconnect the physical grid meter.
        "/Ac/In/0/Connected": 0,
    }


@pytest.fixture
def victron():
    with (
        patch.object(VictronDBus, "_discover_services"),
        patch.object(VictronDBus, "_load_battery_daily_energy"),
    ):
        instance = VictronDBus(test_mode=True)
    instance._tz_name = ZONE
    instance._grid_energy_timezone = ZONE
    instance._grid_telemetry.replace(system_fields())
    instance._native = MagicMock()
    instance._native.get_values_connected.return_value = readings() | {"/NrOfPhases": 2}
    return instance


def refresh(victron):
    victron._refresh_grid_meter(victron._grid_telemetry.generation)


def test_existing_native_tree_read_populates_energy_without_extra_queries(victron):
    with patch.object(victron, "_reconciliation_fallback") as fallback:
        refresh(victron)
    victron._native.get_values_connected.assert_called_once_with(METER, timeout=0.25)
    victron._native.get_value.assert_not_called()
    fallback.assert_not_called()
    assert victron.get_grid_daily_energy()["status"] == "partial"
    assert victron.get_grid_daily_energy()["source"]["device_instance"] == 40
    assert victron.get_grid_daily_energy()["import_kwh"] == 0


def test_cache_getter_performs_no_dbus_or_file_io(victron):
    refresh(victron)
    victron._native.reset_mock()
    with (
        patch("inverter_control.grid_energy.os.open", side_effect=AssertionError("disk read")),
        patch.object(victron._grid_energy, "persist", side_effect=AssertionError("disk write")),
    ):
        assert victron.get_grid_daily_energy()["status"] == "partial"
    victron._native.get_values_connected.assert_not_called()
    victron._native.get_value.assert_not_called()
    victron._native.call_busitem.assert_not_called()


@pytest.mark.parametrize(
    "invalidator",
    [
        lambda v: v._on_name_owner_changed(METER, ":1.1", ""),
        lambda v: v._on_name_owner_changed(SYSTEM_SERVICE, ":1.1", ":1.2"),
        lambda v: v._apply_fast_value(METER, "/Connected", "0"),
        lambda v: v._apply_fast_value(SYSTEM_SERVICE, "/Ac/In/0/ServiceName", METER + "_other"),
    ],
)
def test_owner_health_or_selection_change_immediately_hides_cached_energy(victron, invalidator):
    refresh(victron)
    invalidator(victron)
    assert victron.get_grid_daily_energy()["status"] == "stale"
    assert victron.get_grid_daily_energy()["import_kwh"] is None


def test_snapshot_crossing_owner_change_cannot_revive_energy(victron):
    refresh(victron)

    def old_reply(_meter, timeout):
        victron._on_name_owner_changed(METER, ":1.1", "")
        return readings(102)

    victron._native.get_values_connected.side_effect = old_reply
    refresh(victron)
    assert victron.get_grid_daily_energy()["status"] == "stale"
    assert victron.get_grid_daily_energy()["import_kwh"] is None


def test_owner_change_after_acceptance_before_ledger_update_cannot_revive_energy(victron):
    refresh(victron)
    real_observe = victron._grid_energy.observe

    def delayed_observe(*args, **kwargs):
        victron._on_name_owner_changed(METER, ":1.1", "")
        real_observe(*args, **kwargs)

    with patch.object(victron._grid_energy, "observe", side_effect=delayed_observe):
        refresh(victron)
    assert victron.get_grid_daily_energy()["status"] == "stale"


def test_power_change_signals_do_not_make_unchanged_energy_stale(victron):
    refresh(victron)
    victron._apply_fast_value(SYSTEM_SERVICE, "/Ac/Grid/L1/Power", "200")
    assert victron.get_grid_daily_energy()["status"] == "partial"


def test_missing_or_ambiguous_source_never_uses_an_arbitrary_meter(victron):
    refresh(victron)
    fields = system_fields() | {
        "/Ac/In/1/Source": 1,
        "/Ac/In/1/ServiceName": METER + "_other",
        "/Ac/In/1/DeviceInstance": 41,
    }
    victron._grid_telemetry.replace(fields)
    refresh(victron)
    assert victron.get_grid_daily_energy()["status"] == "stale"
    assert victron.get_grid_daily_energy()["import_kwh"] is None


def test_duplicate_reference_to_same_physical_meter_is_allowed(victron):
    fields = system_fields() | {
        "/Ac/In/1/Source": 1,
        "/Ac/In/1/ServiceName": METER,
        "/Ac/In/1/DeviceInstance": 40,
    }
    victron._grid_telemetry.replace(fields)
    refresh(victron)
    assert victron.get_grid_daily_energy()["status"] == "partial"


def test_meter_instance_mismatch_is_unavailable(victron):
    victron._native.get_values_connected.return_value = readings(**{"/DeviceInstance": 41})
    refresh(victron)
    assert victron.get_grid_daily_energy()["import_kwh"] is None


@pytest.mark.parametrize(
    "reply", [None, readings(**{"/Connected": 0}), readings(**{"/Ac/Energy/Reverse": None})]
)
def test_failed_or_incomplete_read_does_not_retain_valid_numbers(victron, reply):
    refresh(victron)
    victron._native.get_values_connected.return_value = reply
    with patch.object(victron, "_reconciliation_fallback", return_value=None):
        refresh(victron)
    assert victron.get_grid_daily_energy()["status"] == "stale"
    assert victron.get_grid_daily_energy()["import_kwh"] is None


def test_cli_fallback_reuses_one_existing_tree_reply(victron):
    victron._native = None
    output = """string "Connected"
variant int32 1
string "NrOfPhases"
variant int32 2
string "DeviceInstance"
variant int32 40
string "Serial"
variant string "TESTMETER001"
string "Ac/Energy/Forward"
variant double 1234.5
string "Ac/Energy/Reverse"
variant double 12.25
"""
    with patch.object(victron, "_reconciliation_fallback", return_value=output) as fallback:
        refresh(victron)
    fallback.assert_called_once()
    assert victron.get_grid_daily_energy()["status"] == "partial"
    assert victron.get_grid_daily_energy()["source"]["serial"] == "TESTMETER001"


def test_initial_timezone_waits_for_meter_without_resetting_saved_interval(victron, tmp_path):
    clock = Clock()
    path = tmp_path / "state.json"
    saved = clock.ledger(path)
    saved.observe(METER, readings(), ZONE)
    clock.advance(5)
    saved.observe(METER, readings(101, 20.25), ZONE)
    saved.persist()
    original = json.loads(path.read_text())

    victron._grid_energy = clock.ledger(path)
    victron._grid_energy_timezone = ""
    victron._set_grid_energy_timezone(ZONE)
    waiting = victron._grid_energy.snapshot()
    assert waiting["status"] == "unknown"
    assert waiting["reason"] == "awaiting_meter"
    assert waiting["import_kwh"] is waiting["export_kwh"] is None
    assert waiting["started_at"] == original["started_at"]
    victron._grid_energy.persist()
    assert json.loads(path.read_text()) == original

    clock.advance(5)
    victron._native.get_values_connected.return_value = readings(102, 20.5)
    with (
        patch("inverter_control.victron.time.time", lambda: clock.wall),
        patch("inverter_control.victron.time.monotonic", lambda: clock.mono),
    ):
        refresh(victron)
    result = victron.get_grid_daily_energy()
    assert result["status"] == "partial"
    assert result["reason"] == "incomplete_day"
    assert result["import_kwh"] == 2
    assert result["export_kwh"] == 0.5
    assert result["started_at"] == original["started_at"]
    victron._grid_energy.persist()
    resumed = json.loads(path.read_text())
    assert resumed["generation"] == original["generation"]
    assert resumed["baseline"] == original["baseline"]
    assert resumed["started_at"] == original["started_at"]


def test_timezone_subscription_does_not_affect_grid_control_health(victron):
    victron._native.subscribe_service_items.side_effect = lambda service, **kwargs: (
        service != SETTINGS_SERVICE
    )
    victron._native.subscribe_busitem.return_value = False
    victron._maintain_timezone_subscriptions()
    with patch.object(victron, "_seed_fast_values"):
        victron._setup_fast_signals()
    assert victron._signal_paths_subscribed
    victron._native.subscribe_service_items.assert_any_call(SETTINGS_SERVICE, required=False)
    victron._native.subscribe_busitem.assert_any_call(
        SETTINGS_SERVICE, TIME_ZONE_PATH, required=False
    )
    refresh(victron)
    victron._apply_fast_value(SETTINGS_SERVICE, TIME_ZONE_PATH, "UTC")
    assert victron.get_grid_daily_energy()["status"] == "reset"
    assert victron.get_grid_daily_energy()["reason"] == "timezone_changed"
    refresh(victron)
    assert victron.get_grid_daily_energy()["status"] == "reset"
    assert victron.get_grid_daily_energy()["reason"] == "timezone_changed"
    refresh(victron)
    assert victron.get_grid_daily_energy()["time_zone"] == "UTC"
    assert victron.get_grid_daily_energy()["status"] == "partial"


def test_settings_owner_loss_rejects_old_timezone_until_existing_reader_reseeds(victron):
    refresh(victron)
    victron._on_name_owner_changed(SETTINGS_SERVICE, ":1.1", "")
    assert victron._tz_name == ZONE
    assert victron._grid_energy_timezone == ""
    assert victron.get_grid_daily_energy()["status"] == "stale"
    assert victron.get_grid_daily_energy()["reason"] == "timezone_unavailable"
    refresh(victron)
    assert victron.get_grid_daily_energy()["import_kwh"] is None


def test_slow_disk_persistence_is_only_explicit_not_part_of_meter_refresh(victron, tmp_path):
    clock = Clock()
    victron._grid_energy = GridEnergyLedger(
        tmp_path / "state.json", wall=lambda: clock.wall, monotonic=lambda: clock.mono
    )
    with patch.object(victron._grid_energy, "persist") as persist:
        refresh(victron)
        victron.get_grid_daily_energy()
        persist.assert_not_called()
        with patch.object(victron, "_maintain_timezone_subscriptions"):
            victron.persist_grid_energy()
        persist.assert_called_once()


def test_background_retries_optional_failure_even_while_control_stays_healthy(victron):
    native = client()
    victron._native = native
    native.subscribe_service_items(SYSTEM_SERVICE)
    native._send_add_match.side_effect = reject_timezone
    victron._maintain_timezone_subscriptions()
    assert native.subscriptions_healthy()
    assert not victron._timezone_subscriptions_ready
    calls = native._send_add_match.call_count
    for _ in range(10):
        victron._maintain_timezone_subscriptions()
    assert native._send_add_match.call_count == calls  # independently throttled
    native._send_add_match.side_effect = None
    native.get_value_connected = Mock(return_value=None)
    victron._next_timezone_subscription_retry = 0
    victron._maintain_timezone_subscriptions()
    assert native.subscriptions_healthy()
    assert native.optional_subscriptions_healthy()
    assert victron._timezone_subscriptions_ready
    assert victron._tz_name == ZONE
    assert victron._grid_energy_timezone == ""  # missed settings changes must be reread
    native.get_value_connected = Mock(return_value="Europe/Paris")
    victron._next_timezone_subscription_retry = 0
    victron._maintain_timezone_subscriptions()
    native.get_value_connected.assert_called_once_with(
        SETTINGS_SERVICE, TIME_ZONE_PATH, timeout=0.25
    )
    assert victron._grid_energy_timezone == "Europe/Paris"
    assert victron._tz_name == ZONE


def test_optional_outage_hides_energy_and_drops_timezone_cache_until_repaired(victron):
    refresh(victron)
    native = client()
    victron._native = native
    native.subscribe_service_items(SYSTEM_SERVICE)
    native.subscribe_service_items(SETTINGS_SERVICE, required=False)
    native.subscribe_busitem(SETTINGS_SERVICE, TIME_ZONE_PATH, required=False)
    victron._timezone_subscriptions_ready = True
    native._send_add_match.side_effect = reject_timezone
    native._replay_subscriptions()
    assert victron.get_grid_daily_energy()["status"] == "stale"
    assert victron.get_grid_daily_energy()["import_kwh"] is None
    victron._maintain_timezone_subscriptions()
    assert victron._tz_name == ZONE
    assert victron._grid_energy_timezone == ""
    assert native.subscriptions_healthy()


def test_timezone_repair_preserves_control_and_battery_cache(victron):
    native = client()
    victron._native = native
    native.subscribe_service_items(SYSTEM_SERVICE)
    native._send_add_match.side_effect = reject_timezone
    with patch.object(victron, "_dbus_get", side_effect=AssertionError("control settings I/O")):
        victron._maintain_timezone_subscriptions()
        assert victron._tz_name == ZONE
        assert victron._local_now().tzinfo.key == ZONE
        victron.get_local_hour()
        victron._on_name_owner_changed(SETTINGS_SERVICE, ":1.1", "")
        assert victron._tz_name == ZONE
        assert victron._local_now().tzinfo.key == ZONE


def test_late_settings_read_cannot_overwrite_newer_timezone_signal(victron):
    def reply(_service, _path, timeout):
        victron._apply_fast_value(SETTINGS_SERVICE, TIME_ZONE_PATH, "UTC")
        return ZONE

    victron._native.get_value_connected.side_effect = reply
    victron._maintain_timezone_subscriptions()
    assert victron._grid_energy_timezone == "UTC"
    assert victron._tz_name == ZONE


def test_grid_poll_never_repairs_optional_subscriptions(victron):
    victron._last_signal_ok_monotonic = None
    with patch.object(
        victron,
        "_maintain_timezone_subscriptions",
        side_effect=AssertionError("optional repair must remain on display worker"),
    ):
        for name in (
            "_poll_grid_backup",
            "_poll_system_data",
            "_poll_shunt_data",
            "_poll_inverter_power",
            "_reconcile_mppt_data",
            "_reconcile_pv_power",
            "_reconcile_acload_power",
            "_poll_battery_chain_socs",
            "_poll_inverter_state",
            "_reconcile_groups_if_stale",
            "_poll_battery_cell_data_tree",
            "_poll_daily_yields",
            "_poll_battery_daily_energy",
        ):
            setattr(victron, name, Mock())
        victron._poll_all()


@pytest.mark.parametrize(
    "gap",
    [
        "none",
        "meter",
        "system",
        "missing_source",
        "ambiguous_source",
        "owner",
        "source_signal",
        "disconnected",
    ],
)
def test_midnight_proof_cannot_span_gap_without_an_intervening_display_read(victron, gap):
    clock = Clock("2026-09-29T23:59:50")
    victron._grid_energy = clock.ledger()
    mode = "valid"

    def reply(service, timeout):
        if service == SYSTEM_SERVICE:
            if mode == "system":
                return None
            if mode == "missing_source":
                return {}
            if mode == "ambiguous_source":
                return system_fields() | {
                    "/Ac/In/1/Source": 1,
                    "/Ac/In/1/ServiceName": METER + "_other",
                    "/Ac/In/1/DeviceInstance": 41,
                }
            return system_fields()
        return None if mode == "meter" else readings() | {"/NrOfPhases": 2}

    victron._native.get_values_connected.side_effect = reply
    with (
        patch("inverter_control.victron.time.time", lambda: clock.wall),
        patch("inverter_control.victron.time.monotonic", lambda: clock.mono),
        patch.object(victron, "_reconciliation_fallback", return_value=None),
    ):
        victron._read_system_snapshot()
        clock.advance(5)
        if gap == "owner":
            victron._on_name_owner_changed(METER, ":1.1", "")
        elif gap == "source_signal":
            victron._apply_fast_value(SYSTEM_SERVICE, "/Ac/In/0/Source", None)
            victron._apply_fast_value(SYSTEM_SERVICE, "/Ac/In/0/Source", "1")
        elif gap == "disconnected":
            victron._apply_fast_value(METER, "/Connected", "0")
            victron._apply_fast_value(METER, "/Connected", "1")
        else:
            mode = gap
            victron._read_system_snapshot()
        # Deliberately no get_grid_daily_energy() call between the gap and recovery.
        clock.advance(15)
        mode = "valid"
        victron._read_system_snapshot()
        result = victron.get_grid_daily_energy()
    assert result["status"] == ("complete" if gap == "none" else "partial")
    assert result["complete"] == (gap == "none")
    assert result["started_at"] == (clock.wall - 10 if gap == "none" else clock.wall)
    assert result["import_kwh"] == result["export_kwh"] == 0


@pytest.mark.parametrize("failed_service", [SYSTEM_SERVICE, METER])
def test_late_failed_reply_does_not_invalidate_a_newer_accepted_energy_read(
    victron, failed_service
):
    clock = Clock()
    victron._grid_energy = clock.ledger()
    newer_read = False

    def reply(service):
        nonlocal newer_read
        if service == failed_service and not newer_read:
            newer_read = True
            clock.advance(5)
            victron._read_system_snapshot()
            return None, clock.mono + 0.1
        return (system_fields() if service == SYSTEM_SERVICE else readings(102)), clock.mono + 0.1

    with (
        patch("inverter_control.victron.time.time", lambda: clock.wall),
        patch("inverter_control.victron.time.monotonic", lambda: clock.mono),
        patch.object(victron, "_reconciliation_fallback", return_value=None),
    ):
        refresh(victron)
        with patch.object(victron, "_native_reconciliation_read", side_effect=reply):
            victron._read_system_snapshot()
        result = victron.get_grid_daily_energy()
    assert result["status"] == "partial"
    assert result["import_kwh"] == 2
    assert result["observed_at"] == clock.wall


def test_selected_instance_mismatch_cannot_establish_midnight_baseline_without_getters(victron):
    clock = Clock("2026-09-29T23:59:50")
    victron._grid_energy = clock.ledger()
    selected_instance = 40

    def reply(service, timeout):
        return (
            system_fields(instance=selected_instance)
            if service == SYSTEM_SERVICE
            else readings() | {"/NrOfPhases": 2}
        )

    victron._native.get_values_connected.side_effect = reply
    with (
        patch("inverter_control.victron.time.time", lambda: clock.wall),
        patch("inverter_control.victron.time.monotonic", lambda: clock.mono),
    ):
        victron._read_system_snapshot()
        clock.advance(5)
        selected_instance = 41  # The physical meter still reports DeviceInstance 40.
        victron._read_system_snapshot()
        clock.advance(15)
        victron._read_system_snapshot()
        clock.advance(10)
        selected_instance = 40
        victron._read_system_snapshot()
        # No getter while mismatched; those replies cannot become a hidden baseline.
        result = victron.get_grid_daily_energy()
    assert result["status"] == "partial"
    assert not result["complete"]
    assert result["started_at"] == clock.wall
    assert result["import_kwh"] == result["export_kwh"] == 0

"""Physical-meter daily counters, coverage and bounded durable state."""

import json
import os
import threading
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from inverter_control.grid_energy import GridEnergyLedger, parse_energy_snapshot

METER = "com.victronenergy.grid.ve_TESTMETER001"
ZONE = "America/Los_Angeles"


def readings(imported=100.0, exported=20.0, **changes):
    return {
        "/Connected": "1",
        "/DeviceInstance": "40",
        "/Serial": "TESTMETER001",
        "/Ac/Energy/Forward": imported,
        "/Ac/Energy/Reverse": exported,
    } | changes


class Clock:
    def __init__(self, local="2026-09-29T12:00:00.125", zone=ZONE):
        self.wall = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone)).timestamp()
        self.mono = 1000.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def ledger(self, path=None):
        return GridEnergyLedger(path, wall=lambda: self.wall, monotonic=lambda: self.mono)


def observe(ledger, imported=100, exported=20, zone=ZONE, **changes):
    ledger.observe(METER, readings(imported, exported, **changes), zone)


def test_initial_partial_tracks_only_physical_total_deltas_and_fractional_read_time():
    clock = Clock()
    ledger = clock.ledger()
    assert ledger.snapshot()["import_kwh"] is None
    observe(ledger, **{"/Ac/L1/Energy/Forward": 100000, "/Ac/Power": 9000})
    baseline = clock.wall
    clock.advance(12)
    observe(ledger, 101.25, 20.02)
    snapshot = ledger.snapshot()
    assert snapshot == {
        "date": "2026-09-29",
        "time_zone": ZONE,
        "import_kwh": 1.25,
        "export_kwh": 0.02,
        "observed_at": clock.wall,
        "started_at": baseline,
        "complete": False,
        "source": {"service": METER, "device_instance": 40, "serial": "TESTMETER001"},
        "status": "partial",
        "reason": "incomplete_day",
    }
    snapshot["source"]["serial"] = "mutated"
    assert ledger.snapshot()["source"]["serial"] == "TESTMETER001"


@pytest.mark.parametrize(
    "change",
    [
        {"/Ac/Energy/Forward": None},
        {"/Ac/Energy/Reverse": "nan"},
        {"/Ac/Energy/Forward": True},
        {"/Ac/Energy/Forward": -1},
        {"/Ac/Energy/Reverse": "inf"},
        {"/Connected": 0},
        {"/DeviceInstance": None},
        {"/Serial": []},
    ],
)
def test_missing_invalid_or_disconnected_meter_is_unknown_not_zero(change):
    ledger = Clock().ledger()
    observe(ledger, **change)
    snapshot = ledger.snapshot()
    assert snapshot["status"] == "unknown"
    assert snapshot["import_kwh"] is snapshot["export_kwh"] is None


def test_serial_is_optional_but_service_must_be_a_physical_grid_meter():
    ledger = Clock().ledger()
    observe(ledger, **{"/Serial": None})
    assert ledger.snapshot()["source"]["serial"] is None
    ledger.observe("com.victronenergy.system", readings(), ZONE)
    assert ledger.snapshot()["status"] == "stale"


def test_unchanged_counters_are_revalidated_by_read_time_not_change_time():
    clock = Clock()
    ledger = clock.ledger()
    observe(ledger)
    clock.advance(90.001)
    assert ledger.snapshot()["status"] == "stale"
    assert ledger.snapshot()["import_kwh"] is None
    observe(ledger)
    assert ledger.snapshot()["observed_at"] == clock.wall
    assert ledger.snapshot()["import_kwh"] == 0
    ledger.invalidate()
    assert ledger.snapshot()["status"] == "stale"
    observe(ledger, 102, 20)
    assert ledger.snapshot()["import_kwh"] == 2  # cumulative counters cover the gap


@pytest.mark.parametrize(
    "local,expected",
    [
        ("2026-12-31T23:59:50", "2027-01-01"),
        ("2026-03-07T23:59:50", "2026-03-08"),
        ("2026-11-01T23:59:50", "2026-11-02"),
    ],
)
def test_unchanged_fresh_midnight_bracket_proves_complete_day(local, expected):
    clock = Clock(local)
    ledger = clock.ledger()
    observe(ledger)
    clock.advance(20)
    observe(ledger)
    result = ledger.snapshot()
    assert result["date"] == expected
    assert result["status"] == "complete"
    assert result["complete"] is True
    assert (
        result["started_at"]
        == datetime.fromisoformat(expected).replace(tzinfo=ZoneInfo(ZONE)).timestamp()
    )
    clock.advance(10)
    observe(ledger, 100.02, 20.01)
    assert ledger.snapshot()["import_kwh"] == 0.02


@pytest.mark.parametrize(
    "gap,imported,invalid",
    [(20, 100.01, False), (31, 100, False), (86420, 100, False), (20, 100, True)],
)
def test_unproven_midnight_or_day_gap_never_allocates_cross_boundary_delta(gap, imported, invalid):
    clock = Clock("2026-09-29T23:59:50")
    ledger = clock.ledger()
    observe(ledger)
    clock.advance(gap)
    if invalid:
        ledger.invalidate()
    observe(ledger, imported)
    assert ledger.snapshot()["status"] == "partial"
    assert ledger.snapshot()["started_at"] == clock.wall
    assert ledger.snapshot()["import_kwh"] == 0


@pytest.mark.parametrize("local", ["2026-03-08T01:59:50", "2026-11-01T01:59:50"])
def test_dst_clock_change_is_not_a_counter_reset(local):
    clock = Clock(local)
    ledger = clock.ledger()
    observe(ledger)
    baseline = clock.wall
    clock.advance(20)
    observe(ledger, 101)
    assert ledger.snapshot()["status"] == "partial"
    assert ledger.snapshot()["started_at"] == baseline
    assert ledger.snapshot()["import_kwh"] == 1


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"/Ac/Energy/Forward": 1}, "counter_reset"),
        ({"/Ac/Energy/Reverse": 1}, "counter_reset"),
        ({"/Serial": "different-meter"}, "source_changed"),
        ({"/DeviceInstance": 41}, "source_changed"),
    ],
)
def test_counter_decrease_or_meter_identity_change_reseeds_partial(change, reason):
    clock = Clock()
    ledger = clock.ledger()
    observe(ledger)
    clock.advance(5)
    observe(ledger, **change)
    reset = ledger.snapshot()
    assert reset["status"] == "reset"
    assert reset["reason"] == reason
    assert reset["import_kwh"] is reset["export_kwh"] is None
    clock.advance(5)
    observe(ledger, **change)
    assert ledger.snapshot()["status"] == "partial"
    assert ledger.snapshot()["started_at"] == reset["started_at"]
    assert ledger.snapshot()["import_kwh"] == 0


@pytest.mark.parametrize("jump", [-60, 3600])
def test_wall_clock_jump_hides_then_reseeds_counters(jump):
    clock = Clock()
    ledger = clock.ledger()
    observe(ledger)
    clock.wall += jump
    assert ledger.snapshot()["status"] == "reset"
    observe(ledger, 101)
    assert ledger.snapshot()["status"] == "reset"
    clock.advance(5)
    observe(ledger, 102)
    assert ledger.snapshot()["status"] == "partial"
    assert ledger.snapshot()["import_kwh"] == 1


def test_timezone_missing_or_changed_cannot_relabel_old_daily_coverage():
    clock = Clock()
    ledger = clock.ledger()
    observe(ledger, zone="")
    assert ledger.snapshot()["status"] == "unknown"
    observe(ledger)
    clock.advance(5)
    observe(ledger, zone="UTC")
    assert ledger.snapshot()["status"] == "reset"
    assert ledger.snapshot()["reason"] == "timezone_changed"
    clock.advance(5)
    observe(ledger, 101, zone="UTC")
    assert ledger.snapshot()["status"] == "partial"
    assert ledger.snapshot()["import_kwh"] == 1


def test_same_day_restart_restores_baseline_only_after_fresh_matching_meter_read(tmp_path):
    clock = Clock()
    path = tmp_path / "persistent" / "state.json"
    ledger = clock.ledger(path)
    observe(ledger)
    ledger.persist()
    assert path.stat().st_mode & 0o777 == 0o600
    restarted = clock.ledger(path)
    assert restarted.snapshot()["status"] == "unknown"
    assert restarted.snapshot()["import_kwh"] is None
    clock.advance(120)
    observe(restarted, 104, 21)
    assert restarted.snapshot()["import_kwh"] == 4
    assert restarted.snapshot()["export_kwh"] == 1
    assert restarted.snapshot()["started_at"] == ledger.snapshot()["started_at"]


def test_restart_crossing_midnight_cannot_prove_complete_even_with_equal_counters(tmp_path):
    clock = Clock("2026-09-29T23:59:50")
    path = tmp_path / "state.json"
    ledger = clock.ledger(path)
    observe(ledger)
    ledger.persist()
    clock.advance(20)
    restarted = clock.ledger(path)
    observe(restarted)
    assert restarted.snapshot()["status"] == "partial"
    assert restarted.snapshot()["started_at"] == clock.wall


def test_complete_day_survives_same_day_restart(tmp_path):
    clock = Clock("2026-09-29T23:59:50")
    path = tmp_path / "state.json"
    ledger = clock.ledger(path)
    observe(ledger)
    clock.advance(20)
    observe(ledger)
    ledger.persist()
    restarted = clock.ledger(path)
    clock.advance(60)
    observe(restarted, 102)
    assert restarted.snapshot()["status"] == "complete"
    assert restarted.snapshot()["import_kwh"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.update(schema=True),
        lambda state: state["baseline"].update(import_kwh="100"),
        lambda state: state["latest"].update(import_kwh="100"),
        lambda state: state["latest"].update(observed_at=True),
        lambda state: state["baseline"].update(export_kwh=float("nan")),
        lambda state: state.update(complete=True),
        lambda state: state.update(date="2025-09-29"),
        lambda state: state["source"].update(device_instance=True),
    ],
)
def test_corrupt_json_state_is_rejected_without_runtime_type_errors(tmp_path, mutation):
    clock = Clock()
    path = tmp_path / "state.json"
    ledger = clock.ledger(path)
    observe(ledger)
    ledger.persist()
    state = json.loads(path.read_text())
    mutation(state)
    path.write_text(json.dumps(state))
    restarted = clock.ledger(path)
    assert restarted.snapshot()["reason"] == "state_unavailable"
    clock.advance(5)
    observe(restarted, 102)
    assert restarted.snapshot()["status"] == "partial"
    assert restarted.snapshot()["import_kwh"] == 0


@pytest.mark.parametrize("kind", ["oversize", "symlink", "fifo"])
def test_load_is_bounded_and_does_not_follow_links_or_wait_for_fifo(tmp_path, kind):
    path = tmp_path / "state.json"
    if kind == "oversize":
        path.write_bytes(b" " * 16385)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("{}")
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    ledger = Clock().ledger(path)
    assert ledger.snapshot()["reason"] == "state_unavailable"


def test_persistence_failure_keeps_previous_file_and_retries_at_most_once_per_minute(tmp_path):
    clock = Clock()
    path = tmp_path / "state.json"
    ledger = clock.ledger(path)
    observe(ledger)
    ledger.persist()
    old = path.read_bytes()
    clock.advance(60)
    observe(ledger, 101)
    with patch(
        "inverter_control.grid_energy.os.replace", side_effect=OSError("disk full")
    ) as replace:
        ledger.persist()
        for _ in range(20):
            ledger.persist()
        assert replace.call_count == 1
    assert path.read_bytes() == old
    assert not list(tmp_path.glob(".grid-energy-*"))
    assert ledger.snapshot()["reason"] == "persistence_unavailable"
    assert ledger.snapshot()["import_kwh"] == 1
    clock.advance(60)
    observe(ledger, 102)
    ledger.persist()
    assert json.loads(path.read_text())["latest"]["import_kwh"] == 102
    assert ledger.snapshot()["reason"] == "incomplete_day"


def test_blocked_disk_write_never_blocks_observations_or_cached_getter(tmp_path):
    clock = Clock()
    ledger = clock.ledger(tmp_path / "state.json")
    observe(ledger)
    entered, release = threading.Event(), threading.Event()

    def blocked_save(_state):
        entered.set()
        assert release.wait(5)

    with patch.object(ledger, "_save", side_effect=blocked_save) as save:
        worker = threading.Thread(target=ledger.persist)
        worker.start()
        try:
            assert entered.wait(2)
            clock.advance(5)
            observe(ledger, 101)
            assert ledger.snapshot()["import_kwh"] == 1
            ledger.persist()  # concurrent writer is skipped, never waits
            assert save.call_count == 1
        finally:
            release.set()
            worker.join(5)
        clock.advance(60)
        ledger.persist()  # newer observation stayed dirty during the first save
        assert save.call_count == 2


def test_cli_tree_uses_total_value_fields_not_text_or_phase_counters():
    output = """string "Ac/Energy/Forward"
variant double 1234.5
string "/Ac/Energy/Reverse"
variant double 12.25
string "DeviceInstance"
variant int32 40
string "Serial"
variant string "TESTMETER001"
string "Connected"
variant int32 1
string "Ac/L1/Energy/Forward"
variant double 999999
"""
    ledger = Clock().ledger()
    ledger.observe(METER, parse_energy_snapshot(output), ZONE)
    assert ledger.snapshot()["status"] == "partial"
    assert ledger.snapshot()["source"]["device_instance"] == 40
    assert parse_energy_snapshot(output)["/Ac/Energy/Forward"] == "1234.5"


def test_temporarily_missing_timezone_at_restart_preserves_same_day_baseline(tmp_path):
    clock = Clock()
    path = tmp_path / "state.json"
    ledger = clock.ledger(path)
    observe(ledger)
    ledger.persist()
    restarted = clock.ledger(path)
    clock.advance(30)
    observe(restarted, 101, zone="")
    assert restarted.snapshot()["import_kwh"] is None
    observe(restarted, 101)
    assert restarted.snapshot()["status"] == "partial"
    assert restarted.snapshot()["import_kwh"] == 1

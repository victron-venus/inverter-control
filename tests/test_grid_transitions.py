"""Selection-edge evidence survives recovery before a control/MQTT snapshot."""

from unittest.mock import MagicMock

import pytest
from test_grid_backup import LOST, PRIMARY, ready_backup, snapshot
from test_main import _make_controller


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("inverter_control.grid_backup.time.monotonic", lambda: now[0])
    monkeypatch.setattr("inverter_control.grid_backup.time.time", lambda: 1000 + now[0])
    return now


def test_prewrite_switch_cause_survives_until_next_mqtt_snapshot(clock, monkeypatch):
    backup = ready_backup()
    controller, victron, _, _ = _make_controller()
    monkeypatch.setattr(controller, "calculate_setpoint", MagicMock(return_value=(0, "")))
    monkeypatch.setattr(controller, "handle_minimize_charging", MagicMock())
    monkeypatch.setattr(controller, "_update_dvcc_limits", MagicMock())
    victron.get_system_data.side_effect = lambda: backup.select(PRIMARY)

    def before_write():
        clock[0] = 100.1
        return backup.select(LOST)

    victron.get_grid_status.side_effect = before_write
    assert controller.run_cycle() is True
    first = controller.get_state_for_mqtt()
    assert first["grid_using_backup"] is False  # Existing early-return behavior.
    assert first["grid_primary_reason"] is None
    clock[0] = 100.2
    assert controller.run_cycle() is True
    recovered = controller.get_state_for_mqtt()
    assert recovered["grid_using_backup"] is True
    assert recovered["grid_primary_reason"] is None  # Instantaneous semantics stay intact.
    event = recovered["grid_source_transitions"][0]
    assert event["primary_reason"] == "offline"
    assert event["observed_at_monotonic"] == 100.1
    assert event["observed_at_unix"] == 1100.1
    assert event["selection_generation"] == 1
    assert event["primary_valid"] is False
    victron.set_grid_setpoint.assert_not_called()
    assert victron.get_grid_status.call_count == 1


def test_filter_style_selection_survives_recovery_and_slim_mqtt(clock, monkeypatch):
    backup = ready_backup()
    controller, victron, _, _ = _make_controller()
    assert controller._grid_ready_for_control(backup.select(PRIMARY)) is True
    # GridFilter invokes select via the cached get_ac_in_power path between cycles.
    clock[0] = 100.1
    failed = backup.select(LOST)
    clock[0] = 100.2
    recovered = backup.select(PRIMARY)
    assert recovered["_grid_primary_reason"] is None
    assert controller._grid_ready_for_control(recovered) is False
    monkeypatch.setattr("inverter_control.controller.MQTT_SLIM_STATE", True)
    event = controller.get_state_for_mqtt()["grid_source_transitions"][0]
    assert event["primary_reason"] == failed["_grid_primary_reason"] == "offline"
    assert event["from_source"] == "primary"
    assert event["to_source"] == backup.service
    victron.set_grid_setpoint.assert_not_called()


def test_recovery_edge_retains_original_failure_and_selection_policy(clock):
    backup = ready_backup()
    backup.select(LOST)
    clock[0] = 101
    assert backup.select(PRIMARY)["_grid_backup"] is True
    clock[0] = 105.9
    backup.replace(snapshot(), backup.generation)
    assert backup.select(PRIMARY)["_grid_backup"] is True
    clock[0] = 106
    recovered = backup.select(PRIMARY)
    assert recovered["_grid_backup"] is False
    assert recovered["_grid_primary_reason"] is None
    events = recovered["_grid_source_transitions"]
    assert len(events) == 2
    assert events[0]["primary_reason"] == "offline"
    assert events[1] == {
        "selection_generation": 2,
        "observed_at_monotonic": 106,
        "observed_at_unix": 1106,
        "from_source": backup.service,
        "to_source": "primary",
        "using_backup": False,
        "primary_valid": True,
        "primary_reason": None,
    }


def test_history_is_bounded_and_only_changes_on_selection_edges(clock):
    backup = ready_backup()
    backup.recovery_seconds = 0
    for _ in range(6):
        backup.select(LOST)
        selected = backup.select(PRIMARY)
    events = selected["_grid_source_transitions"]
    assert [event["selection_generation"] for event in events] == list(range(5, 13))
    assert len(events) == 8
    for _ in range(20):
        assert backup.select(PRIMARY)["_grid_source_transitions"] == events


def test_snapshot_and_public_history_mutations_cannot_rewrite_retained_events(clock):
    backup = ready_backup()
    first = backup.select(LOST)
    first["_grid_source_transitions"][0]["primary_reason"] = "tampered"
    first["_grid_source_transitions"].clear()
    second = backup.select(PRIMARY)
    controller, _, _, _ = _make_controller()
    controller._grid_ready_for_control(second)
    public = controller.get_state_for_mqtt()["grid_source_transitions"]
    public[0]["primary_reason"] = "public mutation"
    assert second["_grid_source_transitions"][0]["primary_reason"] == "offline"
    assert backup.select(PRIMARY)["_grid_source_transitions"][0]["primary_reason"] == "offline"


@pytest.mark.parametrize("enabled", [False, True])
def test_normal_primary_does_not_create_events(clock, enabled):
    backup = ready_backup(enabled=enabled)
    for _ in range(5):
        assert backup.select(PRIMARY)["_grid_source_transitions"] == []
    if not enabled:
        assert backup.select(LOST)["_grid_source_transitions"] == []


def test_backup_loss_is_not_mislabeled_primary_recovery(clock):
    backup = ready_backup()
    backup.select(LOST)
    backup.invalidate()
    result = backup.select(LOST)
    assert result["_grid_valid"] is False
    event = result["_grid_source_transitions"][-1]
    assert event["using_backup"] is False
    assert event["primary_valid"] is False
    assert event["primary_reason"] == "offline"


def test_history_text_is_bounded_without_changing_instantaneous_reason(clock):
    backup = ready_backup()
    reason = "failure " * 1000
    source = "source" * 1000
    backup.service = "backup" * 1000
    selected = backup.select({**LOST, "_grid_source": source, "_grid_invalid_reason": reason})
    assert selected["_grid_primary_reason"] == reason
    event = selected["_grid_source_transitions"][0]
    assert event["primary_reason"] == reason[:512]
    assert event["from_source"] == source[:512]
    assert event["to_source"] == backup.service[:512]


def test_history_does_not_keep_mutable_input_values(clock):
    backup = ready_backup()
    unsupported = {"mutable": []}
    selected = backup.select(
        {**LOST, "_grid_source": unsupported, "_grid_invalid_reason": unsupported}
    )
    unsupported["mutable"].append("changed after selection")
    event = selected["_grid_source_transitions"][0]
    assert event["primary_reason"] is None
    assert event["from_source"] is None
    assert all(
        value is None or isinstance(value, (str, int, float, bool)) for value in event.values()
    )

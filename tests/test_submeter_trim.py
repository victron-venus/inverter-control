"""Deterministic tests for delayed submeter correction and write acceptance."""

import dataclasses
import itertools
import json
import math

import pytest

from inverter_control.submeter_trim import SubmeterTrim

SERVICE = "com.victronenergy.acload.submeter"
SOURCE = ("com.victronenergy.grid.primary", 30, 2)
EPOCH = 1_800_000_000.0


def report(t, power=24, **changes):
    return {
        "available": True,
        "service": SERVICE,
        "device_instance": 42,
        "generation": 0,
        "power": power,
        "measurement_time": EPOCH + t,
        "age_seconds": 0.0,
        **changes,
    }


def propose(trim, t, power=24, previous=-500, **changes):
    args = {
        "now": float(t),
        "wall_time": EPOCH + t,
        "previous_setpoint": previous,
        "base_setpoint": previous,
        "sample": report(t, power),
        "eligible": True,
        "reason": "normal_hold",
        "raw_grid": 0.0,
        "filtered_grid": 0.0,
        "min_setpoint": -2300,
        "max_setpoint": 2250,
        "source_key": SOURCE,
    }
    args.update(changes)
    return trim.propose(**args)


def ready(trim=None, *, power=24, previous=-500):
    trim = trim or SubmeterTrim(True, expected_service=SERVICE)
    decision = None
    for t in (0, 3, 6, 9, 12, 15, 18):
        decision = propose(trim, t, power, previous)
    return trim, decision


def accept(trim, decision, t):
    assert trim.commit(decision, float(t), EPOCH + t)
    return decision.setpoint


def test_default_disabled_passes_existing_calculation():
    trim = SubmeterTrim()
    decision = propose(trim, 0, base_setpoint=-600)
    assert (decision.setpoint, decision.delta, decision.reason) == (-600, 0, "disabled")
    assert trim.status()["enabled"] is False


def test_one_small_step_after_unique_settled_window_is_immutable():
    trim, decision = ready()
    assert (decision.setpoint, decision.delta, decision.reason) == (-504, -4, "trim")
    assert decision.measurement_time == EPOCH + 18
    assert trim.status()["sample_count"] == 5
    assert trim.status()["total_trim"] == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.delta = -99


@pytest.mark.parametrize("power,delta", [(24, -4), (-24, 4), (6, -2), (-6, 2), (5, 0), (-5, 0)])
def test_error_sign_gain_and_deadzone(power, delta):
    _, decision = ready(power=power)
    assert decision.delta == delta


def test_repeated_fast_control_cadence_never_fabricates_source_reports():
    trim = SubmeterTrim(True)
    last = None
    for tick in range(61):
        t = tick / 3
        last = propose(trim, t, sample=report(18 if t >= 18 else 0, age_seconds=t % 18))
    assert last.delta == 0
    assert trim.status()["sample_count"] <= 1


def test_failed_write_retries_same_object_without_consuming_window():
    trim, decision = ready()
    for t in (18.33, 18.66, 19.0, 19.33):
        retry = propose(trim, t, sample=report(18, age_seconds=t - 18))
        assert retry is decision
        assert trim.status()["total_trim"] == 0
        assert trim.status()["sample_count"] == 5
        assert trim.status()["last_accepted_time"] is None
    accept(trim, decision, 19.33)
    assert trim.status()["total_trim"] == -4
    assert trim.status()["sample_count"] == 0
    assert trim.status()["last_accepted_delta"] == -4
    assert not trim.commit(decision, 19.5, EPOCH + 19.5)


def test_retry_discarded_on_new_report_and_old_decision_cannot_commit():
    trim, old = ready()
    current = propose(trim, 21)
    assert current is not old
    assert current.delta == -4
    assert not trim.commit(old, 21, EPOCH + 21)
    assert trim.status()["total_trim"] == 0
    accept(trim, current, 21)
    assert trim.status()["total_trim"] == -4


def test_pending_retry_cannot_bypass_a_new_actuator_limit():
    trim, old = ready()
    new = propose(trim, 18.33, sample=report(18), min_setpoint=-502)
    assert new.delta == 0
    assert not trim.commit(old, 18.33, EPOCH + 18.33)


def test_same_timestamp_with_changed_payload_is_rejected():
    trim, pending = ready()
    decision = propose(trim, 18.33, sample=report(18, power=25))
    assert decision.reason == "changed_submeter_report"
    assert decision.delta == 0
    assert not trim.commit(pending, 18.33, EPOCH + 18.33)


def test_out_of_order_reports_do_not_replace_high_watermark():
    trim, pending = ready()
    old = propose(trim, 19, sample=report(17, age_seconds=2))
    assert old.reason == "out_of_order_submeter"
    assert not trim.commit(pending, 19, EPOCH + 19)
    assert trim.status()["last_measurement_time"] == EPOCH + 18
    repeated = propose(trim, 19.33, sample=report(18, age_seconds=1.33))
    assert repeated.delta == 0
    assert trim.status()["sample_count"] == 0


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"available": False}, "submeter_unavailable"),
        ({"available": 1}, "submeter_unavailable"),
        ({"power": math.nan}, "invalid_submeter"),
        ({"power": math.inf}, "invalid_submeter"),
        ({"power": True}, "invalid_submeter"),
        ({"power": "24"}, "invalid_submeter"),
        ({"measurement_time": False}, "invalid_submeter"),
        ({"measurement_time": None}, "invalid_submeter"),
        ({"measurement_time": EPOCH + 40}, "future_submeter"),
        ({"measurement_time": EPOCH + 8}, "stale_submeter"),
        ({"age_seconds": 9}, "stale_submeter"),
        ({"age_seconds": -1}, "invalid_submeter"),
        ({"age_seconds": True}, "invalid_submeter"),
        ({"service": None}, "invalid_identity"),
        ({"service": "com.victronenergy.acload.foreign"}, "foreign_identity"),
        ({"device_instance": 43}, "foreign_identity"),
        ({"device_instance": False}, "invalid_identity"),
        ({"generation": True}, "invalid_identity"),
    ],
)
def test_invalid_stale_and_foreign_sample_cancels_pending(changes, reason):
    trim, pending = ready()
    current = propose(trim, 19, sample=report(19, **changes))
    assert current.delta == 0
    assert current.reason == reason
    assert not trim.commit(pending, 19, EPOCH + 19)
    assert trim.status()["sample_count"] == 0
    json.dumps(trim.status(), allow_nan=False)


def test_failed_cycle_reset_preserves_budget_and_requires_fresh_warmup():
    trim, decision = ready()
    previous = accept(trim, decision, 18)
    trim.reset("write_failed")
    assert trim.status()["total_trim"] == -4
    decision = propose(trim, 19, previous=previous, sample=report(18, age_seconds=1))
    assert decision.delta == 0
    assert trim.status()["sample_count"] == 0
    assert trim.status()["total_trim"] == -4


def test_outage_preserves_budget_and_recovers_only_with_new_window():
    trim, decision = ready()
    previous = accept(trim, decision, 18)
    propose(trim, 21, previous=previous, sample=report(21, available=False))
    assert trim.status()["total_trim"] == -4
    for t in (24, 27, 30, 33, 36, 39):
        assert propose(trim, t, previous=previous).delta == 0
    decision = propose(trim, 42, previous=previous)
    assert decision.delta == -4


@pytest.mark.parametrize("change", ["backup_generation", "primary_source"])
def test_source_epoch_change_cancels_old_window_and_rewarms(change):
    trim, pending = ready()
    changes = (
        {"sample": report(21, generation=1)}
        if change == "backup_generation"
        else {"source_key": (SOURCE[0], 30, 3)}
    )
    decision = propose(trim, 21, **changes)
    assert decision.delta == 0
    assert trim.status()["sample_count"] == 0
    assert not trim.commit(pending, 21, EPOCH + 21)


@pytest.mark.parametrize("source", [None, (), ("foreign", 1), (SOURCE[0], True)])
def test_missing_or_malformed_primary_source_is_rejected(source):
    trim, _ = ready()
    assert propose(trim, 21, source_key=source).reason == "invalid_source"


def test_special_mode_and_fast_command_take_precedence():
    trim, pending = ready()
    decision = propose(trim, 19, eligible=False, reason="only_charging", base_setpoint=0)
    assert (decision.setpoint, decision.delta, decision.reason) == (0, 0, "only_charging")
    assert not trim.commit(pending, 19, EPOCH + 19)
    accept(trim, decision, 19)
    assert trim.status()["total_trim"] == 0


def test_accepted_fast_command_resets_budget_without_consuming_pending_on_failure():
    trim, decision = ready()
    previous = accept(trim, decision, 18)
    fast = propose(trim, 21, previous=previous, base_setpoint=-600)
    assert fast.reason == "fast_command"
    assert trim.status()["total_trim"] == -4
    assert trim.status()["last_accepted_setpoint"] == -504
    accept(trim, fast, 21)
    assert trim.status()["total_trim"] == 0
    assert trim.status()["last_accepted_setpoint"] == -600


def test_post_command_reports_must_be_measured_after_transport_and_settle_guard():
    trim, decision = ready()
    previous = accept(trim, decision, 18)
    for t, source_t in ((28, 20), (29, 23), (30, 23)):
        current = propose(
            trim,
            t,
            previous=previous,
            sample=report(source_t, age_seconds=t - source_t),
        )
        assert current.delta == 0
        assert trim.status()["sample_count"] == 0
    assert propose(trim, 31, previous=previous, sample=report(24, age_seconds=7)).delta == 0
    assert trim.status()["sample_count"] == 1


@pytest.mark.parametrize("power,previous", [(24, -2298), (-24, 2248), (24, -2296)])
def test_limits_are_not_clipped_or_reached_by_slow_trim(power, previous):
    _, decision = ready(power=power, previous=previous)
    assert decision.delta == 0
    assert decision.reason == "setpoint_limit"
    assert decision.setpoint == previous


def test_noisy_submeter_window_is_discarded():
    trim = SubmeterTrim(True)
    for t, power in ((0, 24), (6, 24), (9, 24), (12, 80)):
        decision = propose(trim, t, power)
    assert decision.reason == "submeter_unstable"
    assert trim.status()["sample_count"] == 0


def test_primary_motion_between_submeter_reports_invalidates_pending_window():
    trim = SubmeterTrim(True)
    for t in (0, 3, 6, 9):
        propose(trim, t)
    decision = propose(trim, 9.33, sample=report(9), raw_grid=31, filtered_grid=31)
    assert decision.reason == "primary_unstable"
    assert trim.status()["sample_count"] == 0


def test_primary_raw_filter_divergence_rejects_a_load_transition():
    trim, pending = ready()
    decision = propose(trim, 18.33, sample=report(18), raw_grid=81)
    assert decision.reason == "primary_transient"
    assert not trim.commit(pending, 18.33, EPOCH + 18.33)
    for t in (21, 24, 27, 30):
        assert propose(trim, t).delta == 0
    assert propose(trim, 33).delta == -4


def test_ordinary_raw_noise_does_not_invalidate_stable_filtered_window():
    trim = SubmeterTrim(True)
    for t in (0, 3, 6, 9, 12, 15, 18):
        decision = propose(trim, t, raw_grid=50 if t % 2 else -50)
    assert decision.delta == -4


def test_widely_spaced_reports_never_form_an_unbounded_window():
    trim = SubmeterTrim(True)
    propose(trim, 0)
    for t in (6, 13, 20, 27, 34, 41):
        assert propose(trim, t).delta == 0
    assert trim.status()["sample_count"] <= 3


def test_total_budget_cannot_be_renewed_by_resets_or_ordinary_outages():
    trim = SubmeterTrim(True)
    previous = -500
    for tick in range(2401):
        t = tick / 3
        measured = math.floor(t / 3) * 3
        decision = propose(
            trim, t, previous=previous, sample=report(measured, age_seconds=t - measured)
        )
        if decision.delta:
            previous = accept(trim, decision, t)
            trim.reset("temporary_skip")
    assert previous == -560
    assert trim.status()["total_trim"] == -60
    assert trim.status()["reason"] in ("trim_budget", "repeated_submeter")


def test_delayed_three_second_closed_loop_settles_without_chasing_repeated_values():
    trim = SubmeterTrim(True)
    previous = -500
    commands = [(0.0, previous)]
    decisions = []
    for tick in range(1801):
        t = tick / 3
        source_t = math.floor(max(0.0, t - 2.5) / 3) * 3
        command_at_measurement = next(
            value for when, value in reversed(commands) if when <= source_t
        )
        measured = 524 + command_at_measurement
        actual_grid = 524 + previous
        decision = propose(
            trim,
            t,
            previous=previous,
            sample=report(source_t, measured, age_seconds=max(0, t - source_t)),
            raw_grid=actual_grid,
            filtered_grid=actual_grid,
        )
        if decision.delta:
            decisions.append((t, decision.delta))
            previous = accept(trim, decision, t)
            commands.append((t, previous))
    assert abs(524 + previous) <= 5
    assert previous >= -524  # No sign reversal or overshoot in this stable-load model.
    assert all(delta < 0 and abs(delta) <= 4 for _, delta in decisions)
    assert all(b[0] - a[0] >= 18 for a, b in itertools.pairwise(decisions))
    assert len(decisions) <= 8
    assert trim.status()["total_trim"] == previous + 500


def test_cloned_or_stale_decision_cannot_be_committed():
    trim, decision = ready()
    assert not trim.commit(dataclasses.replace(decision), 18, EPOCH + 18)
    assert not trim.commit(decision, 17, EPOCH + 17)
    assert not trim.commit(decision, math.nan, EPOCH + 18)
    assert trim.status()["total_trim"] == 0


@pytest.mark.parametrize("field", ["raw_grid", "filtered_grid", "min_setpoint", "max_setpoint"])
@pytest.mark.parametrize("bad", [True, math.nan, math.inf, None, "0"])
def test_nonfinite_boolean_and_malformed_control_values_do_not_trim(field, bad):
    trim, _ = ready()
    assert propose(trim, 21, **{field: bad}).delta == 0


def test_reset_invalidates_pending_but_retains_pinned_identity():
    trim, pending = ready()
    trim.reset("mode_change")
    assert not trim.commit(pending, 19, EPOCH + 19)
    assert propose(trim, 21, sample=report(21, device_instance=43)).reason == "foreign_identity"


def test_clock_reversal_and_invalid_base_do_not_change_actuator():
    trim, _ = ready()
    assert propose(trim, 17).reason == "clock_reversed"
    assert propose(trim, 21, base_setpoint=True).setpoint == -500
    with pytest.raises(ValueError):
        propose(trim, 22, previous=True)

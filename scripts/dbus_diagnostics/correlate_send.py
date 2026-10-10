"""Offline sender/serial joins with signed, explicitly observational send stages."""

# Exact wire schemas reject bools/subclasses; explicit guards keep rejection paths clear.
# pylint: disable=unidiomatic-typecheck,too-many-boolean-expressions

import argparse
import collections
import json
import math
import re
from pathlib import Path

from .send_timing_parser import (
    identifier,
    load_json,
    number,
    schema_name,
    validate_timing,
)


class ClockMap:
    """Endpoint interpolation, not an independent detector of intervening jumps."""

    def __init__(self, before, after):
        fields = {
            "monotonic_before",
            "monotonic_after",
            "realtime",
            "offset_min",
            "offset_max",
        }
        self.brackets = [before, after]
        for row in self.brackets:
            if (
                type(row) is not dict
                or set(row) != fields
                or not all(number(v, False, signed=True) for v in row.values())
                or row["monotonic_before"] < 0
                or row["monotonic_after"] < row["monotonic_before"]
                or row["offset_max"] < row["offset_min"]
                or not math.isclose(
                    row["offset_min"],
                    row["realtime"] - row["monotonic_after"],
                    rel_tol=0,
                    abs_tol=1e-6,
                )
                or not math.isclose(
                    row["offset_max"],
                    row["realtime"] - row["monotonic_before"],
                    rel_tol=0,
                    abs_tol=1e-6,
                )
            ):
                raise ValueError("Invalid clock bracket")
        self.centers = [
            row["monotonic_before"] / 2 + row["monotonic_after"] / 2 for row in self.brackets
        ]
        self.offsets = [row["offset_min"] / 2 + row["offset_max"] / 2 for row in self.brackets]
        if (
            before["monotonic_after"] >= after["monotonic_before"]
            or before["realtime"] >= after["realtime"]
        ):
            raise ValueError("Clock brackets are not chronologically separated")
        derived = (
            self.centers
            + self.offsets
            + [
                (self.offsets[1] - self.offsets[0]) * 1000,
                *[(r["offset_max"] - r["offset_min"]) * 500 for r in self.brackets],
            ]
        )
        if not all(number(x, False, signed=True) for x in derived):
            raise ValueError("Clock arithmetic is not finite")

    def realtime(self, mono):
        if mono is None or not self.centers[0] <= mono <= self.centers[1]:
            return None  # Never silently extrapolate clocks for late/old records.
        fraction = (mono - self.centers[0]) / (self.centers[1] - self.centers[0])
        value = mono + self.offsets[0] + fraction * (self.offsets[1] - self.offsets[0])
        return value if number(value, False, signed=True) else None

    def evidence(self):
        return {
            "brackets": self.brackets,
            "offset_bracket_half_width_max_ms": max(
                r["offset_max"] - r["offset_min"] for r in self.brackets
            )
            * 500,
            "offset_midpoint_drift_ms": (self.offsets[1] - self.offsets[0]) * 1000,
            "mapping": "Linear interpolation of endpoint offset midpoints, without extrapolation.",
            "uncertainty_limit": "Bracket widths describe endpoint sampling only; intervening clock jumps and observer scheduling are not bounded by them.",
        }


class CaptureWindow:
    """Actual capture membership, separate from the wider clock-mapping domain."""

    def __init__(self, capture, clock):
        start, finish = capture.get("start"), capture.get("finish")
        if (
            type(start) is not dict
            or set(start) != {"monotonic", "realtime", "duration_seconds"}
            or type(finish) is not dict
            or set(finish) != {"monotonic", "realtime"}
            or not all(number(v, False) for v in (*start.values(), *finish.values()))
            or start["duration_seconds"] <= 0
            or finish["monotonic"] <= start["monotonic"]
            or finish["realtime"] <= start["realtime"]
        ):
            raise ValueError("Missing or invalid capture start/finish window")
        self.start = start["monotonic"]
        nominal_end = self.start + start["duration_seconds"]
        if not number(nominal_end, False):
            raise ValueError("Capture window arithmetic is not finite")
        # finish includes observer cleanup in existing captures. It must not
        # extend the nominal measurement window; an early finish may shorten it.
        self.end = min(nominal_end, finish["monotonic"])
        self.realtime_start = clock.realtime(self.start)
        self.realtime_end = clock.realtime(self.end)
        if self.realtime_start is None or self.realtime_end is None:
            raise ValueError("Capture window is outside clock mapping bounds")
        self.start_record, self.finish_record = start, finish

    def contains_monotonic(self, stamp):
        return stamp is not None and self.start <= stamp < self.end

    def contains_realtime(self, stamp):
        return self.realtime_start <= stamp < self.realtime_end

    def evidence(self):
        return {
            "start": self.start_record,
            "finish": self.finish_record,
            "monotonic_start_inclusive": self.start,
            "monotonic_end_exclusive": self.end,
            "mapped_realtime_start_inclusive": self.realtime_start,
            "mapped_realtime_end_exclusive": self.realtime_end,
            "membership": "[start.monotonic, min(start.monotonic + duration_seconds, finish.monotonic)); finish cleanup never extends the nominal window.",
            "clock_limit": "Monitor membership uses the same interpolated clock map; endpoint mapping remains conditional on no unobserved clock jump.",
        }


def distribution(values):
    values = sorted(v for v in values if v is not None)
    return {
        "samples": len(values),
        **{
            name: round(values[max(0, math.ceil(len(values) * p) - 1)], 3) if values else None
            for name, p in (
                ("min", 0),
                ("p50", 0.5),
                ("p95", 0.95),
                ("p99", 0.99),
                ("max", 1),
            )
        },
    }


def delta(a, b):
    """Signed observer differences; missing endpoints remain unavailable."""
    if a is None or b is None:
        return None
    result = (b - a) * 1000
    return round(result, 3) if number(result, False, signed=True) else None


def annotated_timing(row):
    # The existing postreader adds exactly this validated annotation. It is not
    # part of the runtime schema, and log emission time is never used as a clock.
    if type(row) is not dict:
        return None
    value = dict(row)
    if "logged_at_unix" in value:
        if not number(value.pop("logged_at_unix"), False):
            return None
    return validate_timing(value)


def monitor_record(row):
    base = {"kind", "monitor_time", "collector_monotonic", "sender", "serial"}
    if (
        type(row) is not dict
        or row.get("kind") not in ("mc", "mr", "err")
        or set(row) != (base if row["kind"] == "mc" else base | {"duration_ms"})
        or type(row["sender"]) is not str
        or re.fullmatch(r":[0-9]+\.[0-9]+", row["sender"]) is None
        or not number(row["monitor_time"], False)
        or not number(row["collector_monotonic"], False)
        or (row["kind"] != "mc" and not number(row["duration_ms"], False))
    ):
        return None
    serial = row["serial"]
    if type(serial) is str and re.fullmatch(r"[0-9]{1,20}", serial):
        serial = int(serial)
    if not identifier(serial, False):
        return None
    return {**row, "serial": serial}


def correlate(capture, logs):
    clock = ClockMap(capture["before"]["clock_bracket"], capture["after"]["clock_bracket"])
    window = CaptureWindow(capture, clock)
    records = capture["dbus"]
    natives, locks = logs["native_calls"], logs["lock_waits"]
    if not all(type(items) is list for items in (records, natives, locks)):
        raise ValueError("Expected record lists")
    if len(records) > 100000 or len(natives) > 10000 or len(locks) > 10000:
        raise ValueError("Offline record limit exceeded")
    expected_pid = capture["before"].get("pids", {}).get("inverter-control")
    if expected_pid is not None and not identifier(expected_pid, False):
        raise ValueError("Invalid capture process identity")
    grouped = collections.defaultdict(list)
    rejected_monitor = []
    for index, row in enumerate(records):
        record = monitor_record(row)
        if record is None:
            rejected_monitor.append({"index": index, "reason": "invalid_monitor_schema"})
        elif (
            not clock.brackets[0]["realtime"]
            <= record["monitor_time"]
            <= clock.brackets[1]["realtime"]
        ):
            rejected_monitor.append({"index": index, "reason": "outside_clock_window"})
        elif not window.contains_realtime(record["monitor_time"]):
            rejected_monitor.append({"index": index, "reason": "outside_capture_window"})
        else:
            grouped[(record["sender"], record["serial"])].append(record)
    parsed = [annotated_timing(row) for row in natives]
    counts = collections.Counter(
        (row["sender"], row["serial"]) for row in parsed if row and schema_name(row) != "write_lock"
    )
    matches, unmatched = [], []
    for index, row in enumerate(parsed):
        reason = None
        if row is None or schema_name(row) == "write_lock":
            unmatched.append({"index": index, "reason": "invalid_native_schema"})
            continue
        key = (row["sender"], row["serial"])
        events = grouped[key]
        calls = [r for r in events if r["kind"] == "mc"]
        replies = [r for r in events if r["kind"] in ("mr", "err")]
        anchors = row.get("anchors", {})
        if None in key:
            reason = "missing_sender_or_serial"
        elif counts[key] != 1:
            reason = "duplicate_native_identity"
        elif any(anchors.get(k) is None for k in ("call_started_at", "returned_at")):
            reason = "native_window_anchors_unavailable"
        elif any(clock.realtime(anchors[k]) is None for k in ("call_started_at", "returned_at")):
            reason = "native_outside_clock_window"
        elif any(
            not window.contains_monotonic(anchors[k]) for k in ("call_started_at", "returned_at")
        ):
            reason = "native_outside_capture_window"
        elif len(calls) != 1 or len(replies) != 1:
            reason = "missing_or_ambiguous_monitor_pair"
        elif replies[0]["monitor_time"] < calls[0]["monitor_time"]:
            reason = "monitor_reply_precedes_call"
        elif not math.isclose(
            replies[0]["duration_ms"],
            (replies[0]["monitor_time"] - calls[0]["monitor_time"]) * 1000,
            rel_tol=0,
            abs_tol=0.0011,
        ):
            reason = "monitor_duration_mismatch"
        elif expected_pid is not None and row.get("pid", expected_pid) != expected_pid:
            reason = "capture_process_mismatch"
        if reason:
            unmatched.append(
                {
                    "index": index,
                    "sender": key[0],
                    "serial": key[1],
                    "reason": reason,
                    "calls": len(calls),
                    "replies": len(replies),
                }
            )
            continue
        call, reply = calls[0], replies[0]

        def a(name, points=anchors):
            return points.get(name)

        def rt(name, anchor=a):
            return clock.realtime(anchor(name))

        parts = {
            "client_call_entry_to_monitor_call_ms": delta(
                rt("call_started_at"), call["monitor_time"]
            ),
            "call_entry_to_send_start_ms": delta(a("call_started_at"), a("send_started_at")),
            "send_sync_ms": row.get("send_sync_ms"),
            "send_return_to_done_observer_ms": row.get("send_return_to_done_observer_ms"),
            "send_start_to_monitor_call_ms": delta(rt("send_started_at"), call["monitor_time"]),
            "send_return_to_monitor_call_ms": delta(rt("send_returned_at"), call["monitor_time"]),
            "send_done_observer_to_monitor_call_ms": delta(
                rt("send_done_observed_at"), call["monitor_time"]
            ),
            "monitor_call_to_reply_ms": delta(call["monitor_time"], reply["monitor_time"]),
            "monitor_reply_to_client_reply_observer_ms": delta(
                reply["monitor_time"], rt("reply_observed_at")
            ),
            "send_done_observer_to_reply_observer_ms": delta(
                a("send_done_observed_at"), a("reply_observed_at")
            ),
            "reply_observer_to_coroutine_resume_ms": row.get("reply_observer_to_resume_ms"),
            "coroutine_cleanup_ms": delta(a("call_finished_at"), a("completed_at")),
            "caller_wakeup_ms": row.get("caller_wakeup_ms"),
        }
        matches.append(
            {
                "sender": key[0],
                "serial": key[1],
                "schema": schema_name(row),
                "pid": row.get("pid"),
                "total_ms": row["total_ms"],
                "monitor_reply_kind": reply["kind"],
                "send_future_cancelled": row.get("send_future_cancelled"),
                "application_ack": "unavailable: timing and profile contain no SetValue return body",
                "anchors_after_caller_return": sorted(
                    name
                    for name, stamp in anchors.items()
                    if stamp is not None and stamp > anchors["returned_at"]
                ),
                "parts": parts,
                "negative_observer_partitions": [
                    name for name, value in parts.items() if value is not None and value < 0
                ],
            }
        )
    parsed_locks = [annotated_timing(row) for row in locks]
    valid_locks, unmatched_locks = [], []
    for index, row in enumerate(parsed_locks):
        if row is None or schema_name(row) != "write_lock":
            reason = "invalid_lock_schema"
        elif expected_pid is not None and row["pid"] != expected_pid:
            reason = "capture_process_mismatch"
        elif not all(window.contains_monotonic(t) for t in row["anchors"].values()):
            reason = "lock_outside_capture_window"
        else:
            valid_locks.append(row)
            continue
        unmatched_locks.append({"index": index, "reason": reason})
    fields = sorted({name for row in matches for name in row["parts"]})
    return {
        "native_records": len(natives),
        "matched_native_records": len(matches),
        "unmatched_native_records": unmatched,
        "rejected_monitor_records": rejected_monitor,
        "input_rejected_timing_lines": logs.get("rejected_timing_lines", 0),
        "rejected_lock_records": len(locks) - len(valid_locks),
        "unmatched_lock_records": unmatched_locks,
        "lock_records": valid_locks,
        "clock": clock.evidence(),
        "capture_window": window.evidence(),
        "phase_distributions_slow_samples_only": {
            name: distribution(row["parts"][name] for row in matches) for name in fields
        },
        "matches": matches,
        "limitations": [
            "Only >=200ms optional, bounded and possibly dropped diagnostic records; these are not all-call percentiles.",
            "All cross-observer partitions stay signed. Monitor scheduling/buffering and offset uncertainty can produce negative values.",
            "Send synchronous duration includes marshalling and possible immediate socket work in dbus-fast 2.21.1; returned-to-done is callback observation delay, not proven queue or kernel latency.",
            "Done/cancelled send Future is not ACK. A profile method return has no body; no per-call successful SetValue or physical response is inferred.",
            "These stage comparisons overlap; do not sum them into a latency budget or subtract independent percentiles.",
            "Clock interpolation assumes no unobserved jump; endpoint brackets do not prove that assumption. No extrapolation is used.",
            "Capture membership is independent of clock-map validity: both native call/return and both monitor events must fall within the explicit capture window. Legacy native records without these anchors remain unmatched.",
            "Sender/serial is scoped to this single capture and bus lifetime; runtime provenance and capture boot/process continuity require separate evidence.",
            "Lock records have no call identity and are never assigned to a native request. Log emission timestamps are ignored.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--timings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = correlate(load_json(args.capture), load_json(args.timings))
    with args.output.open("x") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()

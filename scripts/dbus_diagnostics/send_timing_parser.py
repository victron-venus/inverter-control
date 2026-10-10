"""Strict offline adapter for legacy, anchored and send-observer timing records.

This module performs no runtime discovery, device access or network operations.
"""

# Exact wire schemas reject bools/subclasses; explicit guards keep rejection paths clear.
# pylint: disable=unidiomatic-typecheck,too-many-boolean-expressions,too-many-return-statements

import argparse
import ast
import json
import math
import re
from pathlib import Path

PREFIX = "Native D-Bus write timing: "
MAX_BYTES = 64 * 1024 * 1024
DURATIONS = {
    "total_ms",
    "setup_ms",
    "dispatch_ms",
    "await_reply_ms",
    "caller_wakeup_ms",
}
LEGACY = DURATIONS | {"sender", "serial"}
EXTRA_DURATIONS = {"call_to_reply_observer_ms", "reply_observer_to_resume_ms"}
ANCHORS = {
    "started_at",
    "submitted_at",
    "dispatched_at",
    "await_started_at",
    "call_started_at",
    "reply_observed_at",
    "call_finished_at",
    "completed_at",
    "returned_at",
}
NATIVE = (
    LEGACY
    | EXTRA_DURATIONS
    | {
        "phase",
        "clock",
        "pid",
        "caller_native_tid",
        "loop_native_tid",
        "anchors",
    }
)
SEND_ANCHORS = {"send_started_at", "send_returned_at", "send_done_observed_at"}
SEND_DURATIONS = {"send_sync_ms", "send_return_to_done_observer_ms"}
SEND_NATIVE = NATIVE | SEND_DURATIONS | {"send_future_cancelled"}
LOCK = {"phase", "clock", "pid", "caller_native_tid", "anchors", "lock_wait_ms"}
PAIRS = {
    "total_ms": ("started_at", "returned_at"),
    "setup_ms": ("started_at", "submitted_at"),
    "dispatch_ms": ("submitted_at", "dispatched_at"),
    "await_reply_ms": ("await_started_at", "completed_at"),
    "caller_wakeup_ms": ("completed_at", "returned_at"),
    "call_to_reply_observer_ms": ("call_started_at", "reply_observed_at"),
    "reply_observer_to_resume_ms": ("reply_observed_at", "call_finished_at"),
    "send_sync_ms": ("send_started_at", "send_returned_at"),
    "send_return_to_done_observer_ms": ("send_returned_at", "send_done_observed_at"),
}


def number(value, optional=True, signed=False):
    if value is None:
        return optional
    try:
        return type(value) in (int, float) and math.isfinite(value) and (signed or value >= 0)
    except OverflowError:
        return False


def identifier(value, optional=True):
    return (optional and value is None) or (type(value) is int and value > 0)


def ordered(anchors, names):
    values = [anchors[name] for name in names if anchors.get(name) is not None]
    return all(a <= b for a, b in zip(values, values[1:]))


def duration_matches(value, start, end, returned):
    # Exactly the producer's unavailable/late-stage rule; a timeout return can
    # precede later coroutine-finally anchors. Do not reject those honest records.
    if start is None or end is None or end < start or end > returned:
        return value is None
    expected = (end - start) * 1000
    return (
        number(expected, False)
        and number(value, False)
        and math.isclose(value, round(expected, 3), rel_tol=0, abs_tol=0.0011)
    )


def schema_name(value):
    return {
        frozenset(LEGACY): "legacy",
        frozenset(NATIVE): "native",
        frozenset(SEND_NATIVE): "send_native",
        frozenset(LOCK): "write_lock",
    }.get(frozenset(value))


def validate_timing(value):
    """Return the original exact-schema record, or None; never add success claims."""
    if type(value) is not dict:
        return None
    schema = schema_name(value)
    if schema is None:
        return None
    if schema == "write_lock":
        anchors = value["anchors"]
        if (
            value["phase"] != "write_lock"
            or value["clock"] != "monotonic"
            or not identifier(value["pid"], False)
            or not identifier(value["caller_native_tid"], False)
            or type(anchors) is not dict
            or set(anchors) != {"lock_requested_at", "lock_acquired_at"}
            or not all(number(x, False) for x in anchors.values())
            or not ordered(anchors, ("lock_requested_at", "lock_acquired_at"))
        ):
            return None
        return (
            value
            if duration_matches(
                value["lock_wait_ms"],
                anchors["lock_requested_at"],
                anchors["lock_acquired_at"],
                anchors["lock_acquired_at"],
            )
            else None
        )
    if value["sender"] is not None and (
        type(value["sender"]) is not str
        or re.fullmatch(r":[0-9]+\.[0-9]+", value["sender"]) is None
    ):
        return None
    if not identifier(value["serial"]) or not all(number(value[k]) for k in DURATIONS):
        return None
    if schema == "legacy":
        return value
    anchors = value["anchors"]
    expected = ANCHORS | SEND_ANCHORS if schema == "send_native" else ANCHORS
    if (
        value["phase"] != "native_call"
        or value["clock"] != "monotonic"
        or not identifier(value["pid"], False)
        or not all(identifier(value[k]) for k in ("caller_native_tid", "loop_native_tid"))
        or type(anchors) is not dict
        or set(anchors) != expected
        or not all(number(x) for x in anchors.values())
        or not all(number(anchors[k], False) for k in ("started_at", "returned_at"))
    ):
        return None
    chains = [
        (
            "started_at",
            "submitted_at",
            "dispatched_at",
            "await_started_at",
            "call_started_at",
            "reply_observed_at",
            "call_finished_at",
            "completed_at",
        ),
        ("started_at", "submitted_at", "returned_at"),
    ]
    if schema == "send_native":
        chains.append(
            (
                "call_started_at",
                "send_started_at",
                "send_returned_at",
                "send_done_observed_at",
                "call_finished_at",
            )
        )
        # Reply observation and queued send-done callbacks may occur in either
        # order. There is deliberately no edge between these two observers.
        chains.append(("send_returned_at", "reply_observed_at"))
        cancelled = value["send_future_cancelled"]
        if cancelled is not None and (
            type(cancelled) is not bool or anchors["send_done_observed_at"] is None
        ):
            return None
    if not all(ordered(anchors, chain) for chain in chains):
        return None
    for name, (a, b) in PAIRS.items():
        if name in value and not duration_matches(
            value[name], anchors[a], anchors[b], anchors["returned_at"]
        ):
            return None
    return value


def parse_timing(line):
    if len(line) > 8192 or PREFIX not in line:
        return None
    try:
        tree = ast.parse(line.split(PREFIX, 1)[1].strip(), mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [ast.literal_eval(key) for key in node.keys]
                if not all(type(key) is str for key in keys) or len(set(keys)) != len(keys):
                    return None
        return validate_timing(ast.literal_eval(tree))
    except (SyntaxError, ValueError, TypeError, RecursionError, OverflowError):
        return None


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def load_json(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Offline input byte limit exceeded")
    return json.loads(
        path.read_text(),
        object_pairs_hook=unique_object,
        parse_constant=lambda _x: (_ for _ in ()).throw(ValueError("Non-finite JSON")),
    )


def parse_file(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Offline input byte limit exceeded")
    result = {"native_calls": [], "lock_waits": [], "rejected_timing_lines": 0}
    with path.open() as stream:
        for index, line in enumerate(stream):
            if index >= 100000:
                raise ValueError("Offline input line limit exceeded")
            record = parse_timing(line)
            if record is None:
                result["rejected_timing_lines"] += int(PREFIX in line)
                continue
            key = "lock_waits" if record.get("phase") == "write_lock" else "native_calls"
            if len(result[key]) >= 10000:
                raise ValueError("Offline timing record limit exceeded")
            result[key].append(record)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = parse_file(args.input)
    with args.output.open("x") as output:
        json.dump(result, output, indent=2, allow_nan=False)
        output.write("\n")


if __name__ == "__main__":
    main()

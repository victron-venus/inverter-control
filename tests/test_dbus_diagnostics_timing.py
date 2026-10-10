"""Isolated diagnostic parser and reader regressions."""

import copy
import math

# Test subprocesses use fixed interpreter argv and no shell.
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.dbus_diagnostics.correlate_send import ClockMap, correlate
from scripts.dbus_diagnostics.send_timing_parser import (
    ANCHORS,
    LEGACY,
    PAIRS,
    PREFIX,
    SEND_DURATIONS,
    load_json,
    parse_timing,
    validate_timing,
)

HERE = Path(__file__).resolve().parent / "fixtures/dbus_diagnostics"


def record():
    anchors = dict(
        zip(
            (
                "started_at",
                "submitted_at",
                "dispatched_at",
                "await_started_at",
                "call_started_at",
                "send_started_at",
                "send_returned_at",
                "send_done_observed_at",
                "reply_observed_at",
                "call_finished_at",
                "completed_at",
                "returned_at",
            ),
            (
                100.0,
                100.01,
                100.02,
                100.03,
                100.05,
                100.06,
                100.2,
                100.21,
                100.39,
                100.41,
                100.42,
                100.5,
            ),
        )
    )
    row = {
        "phase": "native_call",
        "clock": "monotonic",
        "pid": 22,
        "caller_native_tid": 22,
        "loop_native_tid": 23,
        "sender": ":1.42",
        "serial": 9,
        "anchors": anchors,
        "send_future_cancelled": False,
    }
    recalculate(row)
    return row


def recalculate(row):
    a = row["anchors"]
    for name, (start, end) in PAIRS.items():
        x, y = a.get(start), a.get(end)
        row[name] = (
            None
            if x is None or y is None or y < x or y > a["returned_at"]
            else round((y - x) * 1000, 3)
        )


def bracket(mono):
    return {
        "monotonic_before": mono - 0.0001,
        "monotonic_after": mono + 0.0001,
        "realtime": mono + 1000,
        "offset_min": 1000 - 0.0001,
        "offset_max": 1000 + 0.0001,
    }


def inputs():
    capture = {
        "before": {"clock_bracket": bracket(99), "pids": {"inverter-control": 22}},
        "after": {"clock_bracket": bracket(102)},
        "start": {"monotonic": 100, "realtime": 1100, "duration_seconds": 1},
        "finish": {"monotonic": 101.1, "realtime": 1101.1},
        "dbus": [
            {
                "kind": "mc",
                "monitor_time": 1100.1,
                "collector_monotonic": 100.11,
                "sender": ":1.42",
                "serial": "9",
            },
            {
                "kind": "mr",
                "monitor_time": 1100.4,
                "collector_monotonic": 100.41,
                "sender": ":1.42",
                "serial": "9",
                "duration_ms": 300.0,
            },
        ],
    }
    return capture, {
        "native_calls": [record()],
        "lock_waits": [],
        "rejected_timing_lines": 0,
    }


class ParserTests(unittest.TestCase):
    def test_actual_dbus_fast_221_fixtures(self):
        data = load_json(HERE / "dbus_fast_2_21_1.json")
        self.assertEqual(data["provenance"]["dbus_fast"], "2.21.1")
        self.assertEqual(len(data["cases"]), 3)
        for item in data["cases"]:
            with self.subTest(item["case"]):
                row = item["record"]
                self.assertEqual(parse_timing(PREFIX + repr(row)), row)
                self.assertEqual(item["original_send_count"], 1)
                self.assertEqual(row["send_future_cancelled"], item["case"] == "cancelled")
                if item["case"] == "queued_done_after_reply_observer":
                    self.assertGreater(
                        row["anchors"]["send_done_observed_at"],
                        row["anchors"]["reply_observed_at"],
                    )

    def test_legacy_old_and_new_exact_schemas(self):
        new = record()
        old = {
            k: copy.deepcopy(v)
            for k, v in new.items()
            if k not in SEND_DURATIONS | {"send_future_cancelled"}
        }
        old["anchors"] = {k: v for k, v in old["anchors"].items() if k in ANCHORS}
        legacy = {k: new[k] for k in LEGACY}
        for row in (new, old, legacy):
            with self.subTest(keys=sorted(row)):
                self.assertEqual(parse_timing("log prefix " + PREFIX + repr(row)), row)

    def test_anchored_records_roundtrip_without_external_captures(self):
        # Older producer schemas remain readable without copying live device logs.
        for index in range(32):
            row = record()
            row["serial"] = index + 1
            row = {
                k: v for k, v in row.items() if k not in SEND_DURATIONS | {"send_future_cancelled"}
            }
            row["anchors"] = {k: v for k, v in row["anchors"].items() if k in ANCHORS}
            self.assertEqual(parse_timing(PREFIX + repr(row)), row)

    def test_malformed_rejected_without_coercion(self):
        cases = []
        for field, value in (
            ("total_ms", True),
            ("serial", True),
            ("pid", 0),
            ("sender", "com.example.Name"),
            ("send_future_cancelled", 0),
            ("send_sync_ms", -1),
            ("send_sync_ms", math.inf),
        ):
            row = record()
            row[field] = value
            cases.append(row)
        for value in (math.nan, -1, True, 10**400):
            row = record()
            row["anchors"]["send_started_at"] = value
            cases.append(row)
        row = record()
        row["extra"] = "not allowed"
        cases.append(row)
        row = record()
        del row["send_sync_ms"]
        cases.append(row)
        row = record()
        row["anchors"]["unknown_at"] = 100
        cases.append(row)
        row = record()
        row["anchors"]["send_done_observed_at"] = None
        recalculate(row)
        cases.append(row)
        for index, row in enumerate(cases):
            with self.subTest(index=index):
                self.assertIsNone(validate_timing(row))

    def test_ordering_and_duration_mismatch_rejected(self):
        for field, value in (
            ("send_returned_at", 100.04),
            ("completed_at", 99),
            ("reply_observed_at", 100.1),
            ("returned_at", 99),
        ):
            row = record()
            row["anchors"][field] = value
            recalculate(row)
            self.assertIsNone(validate_timing(row))
        row = record()
        row["send_sync_ms"] += 1
        self.assertIsNone(validate_timing(row))

    def test_late_finally_after_timeout_and_missing_observation_are_valid(self):
        row = record()
        row["anchors"].update(returned_at=100.3, reply_observed_at=None, send_done_observed_at=None)
        row["send_future_cancelled"] = None
        recalculate(row)
        self.assertIsNone(row["await_reply_ms"])
        self.assertIsNone(row["caller_wakeup_ms"])
        self.assertEqual(validate_timing(row), row)

    def test_lock_separate_and_inconsistent_wait_rejected(self):
        row = {
            "phase": "write_lock",
            "clock": "monotonic",
            "pid": 22,
            "caller_native_tid": 22,
            "anchors": {"lock_requested_at": 10.0, "lock_acquired_at": 10.25},
            "lock_wait_ms": 250.0,
        }
        self.assertEqual(validate_timing(row), row)
        row["lock_wait_ms"] = 1
        self.assertIsNone(validate_timing(row))

    def test_duplicate_literal_and_json_keys_and_provenance_rejected(self):
        line = PREFIX + repr(record())
        self.assertIsNone(parse_timing(line[:-1] + ", 'serial': 10}"))
        self.assertIsNone(parse_timing(PREFIX + "{'phase':'writer_provenance'}"))
        self.assertIsNone(parse_timing(PREFIX + "__import__('os').getpid()"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.json"
            for text in ('{"x":1,"x":2}', '{"x":NaN}'):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    load_json(path)

    def test_cli_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "in.log", Path(temp) / "out.json"
            source.write_text(PREFIX + repr(record()) + "\n")
            cmd = [
                sys.executable,
                "-m",
                "scripts.dbus_diagnostics.send_timing_parser",
                "--input",
                str(source),
                "--output",
                str(output),
            ]
            # Fixed module argv; source/output are isolated temporary files.
            subprocess.run(cmd, check=True, capture_output=True)  # nosec B603
            frozen = output.read_bytes()
            result = subprocess.run(cmd, capture_output=True, check=False)  # nosec B603
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), frozen)


class CorrelationTests(unittest.TestCase):
    def test_signed_partitions_not_clamped_or_acknowledged(self):
        capture, logs = inputs()
        row = correlate(capture, logs)["matches"][0]
        self.assertEqual(row["parts"]["send_return_to_monitor_call_ms"], -100.0)
        self.assertEqual(row["parts"]["monitor_reply_to_client_reply_observer_ms"], -10.0)
        self.assertIn("unavailable", row["application_ack"])
        self.assertFalse(row["send_future_cancelled"])

    def test_future_cancellation_and_method_return_do_not_claim_ack(self):
        capture, logs = inputs()
        logs["native_calls"][0]["send_future_cancelled"] = True
        result = correlate(capture, logs)
        row = result["matches"][0]
        self.assertTrue(row["send_future_cancelled"])
        self.assertIn("unavailable", row["application_ack"])

    def test_legacy_parseable_but_not_joined_without_native_window_anchors(self):
        capture, logs = inputs()
        logs["native_calls"][0] = {k: v for k, v in record().items() if k in LEGACY}
        result = correlate(capture, logs)
        self.assertEqual(result["matched_native_records"], 0)
        self.assertEqual(
            result["unmatched_native_records"][0]["reason"],
            "native_window_anchors_unavailable",
        )

    def test_duplicate_native_or_monitor_identity_never_arbitrarily_joined(self):
        for duplicate_native in (True, False):
            capture, logs = inputs()
            if duplicate_native:
                logs["native_calls"].append(copy.deepcopy(logs["native_calls"][0]))
            else:
                capture["dbus"].append(copy.deepcopy(capture["dbus"][0]))
            result = correlate(capture, logs)
            self.assertEqual(result["matched_native_records"], 0)
            self.assertTrue(result["unmatched_native_records"])

    def test_bad_monitor_pair_and_process_identity_rejected(self):
        for mutation in ("reversed", "duration", "pid", "sender", "extra"):
            capture, logs = inputs()
            if mutation == "reversed":
                capture["dbus"][1]["monitor_time"] = 1100.0
            elif mutation == "duration":
                capture["dbus"][1]["duration_ms"] = 1
            elif mutation == "pid":
                logs["native_calls"][0]["pid"] = 999
            elif mutation == "sender":
                capture["dbus"][1]["sender"] = ":1.43"
            else:
                capture["dbus"][0]["payload"] = "not allowlisted"
            with self.subTest(mutation=mutation):
                self.assertEqual(correlate(capture, logs)["matched_native_records"], 0)

    def test_clock_validation_no_extrapolation(self):
        clock = ClockMap(bracket(99), bracket(102))
        self.assertIsNone(clock.realtime(98))
        self.assertIsNone(clock.realtime(103))
        for bad in (
            bracket(98),
            {**bracket(102), "offset_min": math.nan},
            {**bracket(102), "offset_max": 2000},
            {**bracket(102), "monotonic_before": 105},
        ):
            with self.assertRaises(ValueError):
                ClockMap(bracket(99), bad)
        capture, logs = inputs()
        logs["native_calls"][0]["anchors"]["returned_at"] = 105
        recalculate(logs["native_calls"][0])
        self.assertEqual(
            correlate(capture, logs)["unmatched_native_records"][0]["reason"],
            "native_outside_clock_window",
        )

    def test_multiple_unique_native_matches_preserve_signed_observations(self):
        for count in (13, 19):
            capture, logs = inputs()
            original_pair = capture["dbus"]
            capture["dbus"], logs["native_calls"] = [], []
            for index in range(count):
                native = record()
                native["serial"] = index + 1
                logs["native_calls"].append(native)
                for original in original_pair:
                    item = copy.deepcopy(original)
                    item["serial"] = str(index + 1)
                    capture["dbus"].append(item)
            result = correlate(capture, logs)
            self.assertEqual(result["matched_native_records"], count)
            self.assertEqual(result["unmatched_native_records"], [])
            self.assertTrue(all(r["negative_observer_partitions"] for r in result["matches"]))

    def test_record_limits(self):
        capture, logs = inputs()
        capture["dbus"] = [None] * 100001
        with self.assertRaises(ValueError):
            correlate(capture, logs)

    def test_missing_or_invalid_capture_window_fails_closed(self):
        for mutation in ("missing", "duration", "boolean", "extra", "reverse", "clock"):
            capture, logs = inputs()
            if mutation == "missing":
                del capture["start"]
            elif mutation == "duration":
                capture["start"]["duration_seconds"] = 0
            elif mutation == "boolean":
                capture["finish"]["monotonic"] = True
            elif mutation == "extra":
                capture["start"]["unused"] = 1
            elif mutation == "reverse":
                capture["finish"]["monotonic"] = 99
            else:
                capture["start"]["monotonic"] = 98
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                correlate(capture, logs)

    def test_pre_start_and_cleanup_events_inside_clock_bounds_are_excluded(self):
        for start, duration in ((100.6, 1), (99.5, 0.5)):
            capture, logs = inputs()
            capture["start"].update(
                monotonic=start, realtime=start + 1000, duration_seconds=duration
            )
            capture["finish"] = {"monotonic": 101.8, "realtime": 1101.8}
            result = correlate(capture, logs)
            self.assertEqual(result["matched_native_records"], 0)
            self.assertEqual(
                result["unmatched_native_records"][0]["reason"],
                "native_outside_capture_window",
            )
            self.assertEqual(
                [r["reason"] for r in result["rejected_monitor_records"]],
                ["outside_capture_window"] * 2,
            )

    def test_native_straddling_and_exact_end_excluded_with_monitor_pair_in_window(self):
        for stamp in (99.99, 101, 101.05):
            capture, logs = inputs()
            row = logs["native_calls"][0]
            if stamp < 100:
                row["anchors"].update(
                    started_at=99.9,
                    submitted_at=99.91,
                    dispatched_at=99.92,
                    await_started_at=99.93,
                    call_started_at=stamp,
                )
            else:
                row["anchors"]["returned_at"] = stamp
            recalculate(row)
            result = correlate(capture, logs)
            self.assertEqual(result["rejected_monitor_records"], [])
            self.assertEqual(result["matched_native_records"], 0)
            self.assertEqual(
                result["unmatched_native_records"][0]["reason"],
                "native_outside_capture_window",
            )

    def test_monitor_straddling_excluded_with_native_entirely_in_window(self):
        for index, stamp in ((0, 1099.99), (1, 1101), (1, 1101.05)):
            capture, logs = inputs()
            capture["dbus"][index]["monitor_time"] = stamp
            capture["dbus"][1]["duration_ms"] = (
                capture["dbus"][1]["monitor_time"] - capture["dbus"][0]["monitor_time"]
            ) * 1000
            result = correlate(capture, logs)
            self.assertEqual(result["matched_native_records"], 0)
            self.assertEqual(
                result["rejected_monitor_records"][0]["reason"],
                "outside_capture_window",
            )

    def test_early_finish_shortens_nominal_window_and_locks_are_scoped(self):
        capture, logs = inputs()
        capture["finish"] = {"monotonic": 100.45, "realtime": 1100.45}
        result = correlate(capture, logs)
        self.assertEqual(result["matched_native_records"], 0)
        self.assertEqual(result["capture_window"]["monotonic_end_exclusive"], 100.45)
        capture, logs = inputs()
        for start, end in ((100.1, 100.35), (99.9, 100.2), (100.8, 101.05)):
            logs["lock_waits"].append(
                {
                    "phase": "write_lock",
                    "clock": "monotonic",
                    "pid": 22,
                    "caller_native_tid": 22,
                    "anchors": {"lock_requested_at": start, "lock_acquired_at": end},
                    "lock_wait_ms": round((end - start) * 1000, 3),
                }
            )
        result = correlate(capture, logs)
        self.assertEqual(len(result["lock_records"]), 1)
        self.assertEqual(result["rejected_lock_records"], 2)
        self.assertTrue(
            all(
                r["reason"] == "lock_outside_capture_window"
                for r in result["unmatched_lock_records"]
            )
        )


if __name__ == "__main__":
    unittest.main()

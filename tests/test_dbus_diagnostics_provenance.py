"""Isolated diagnostic parser and reader regressions."""

import copy
import hashlib
import json
import math
import os

# Test subprocesses use fixed interpreter argv and no shell.
import subprocess  # nosec B404
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.dbus_diagnostics import provenance_parser as parser
from scripts.dbus_diagnostics import read_provenance as reader

HERE = Path(__file__).resolve().parent / "fixtures/dbus_diagnostics"


def fixture():
    return json.loads((HERE / "writer_provenance_2_21_1.json").read_text())["record"]


def stamp(value=1000):
    return "@" + format(value + 0x400000000000000A, "016x") + "00000000"


class ProvenanceSchemaTests(unittest.TestCase):
    def test_actual_221_fixture_and_optional_compiled_code(self):
        row = fixture()
        self.assertEqual(parser.parse_provenance(parser.PREFIX + repr(row)), row)
        self.assertEqual(row["underlying_send"]["code"]["status"], "captured_python_code")
        for status in ("no_python_code", "code_budget_exceeded"):
            row["underlying_send"]["code"] = {"status": status}
            self.assertEqual(parser.validate_provenance(row), row)

    def test_all_status_variants_are_exact(self):
        row = fixture()
        original = next(m for m in row["modules"] if m["module"] == "inverter_control.dbus_native")
        for status in (
            "module_not_loaded",
            "no_bounded_file_path",
            "not_regular_file",
            "file_budget_exceeded",
            "file_changed_during_read",
            "file_unavailable",
        ):
            value = {
                "module": original["module"],
                "file": None if status == "module_not_loaded" else original["file"],
                "spec_origin": None if status == "module_not_loaded" else original["spec_origin"],
                "status": status,
            }
            if status == "file_unavailable":
                value["error_type"] = "FileNotFoundError"
            with self.subTest(status=status):
                self.assertTrue(parser.module(value))
                value["unexpected"] = 1
                self.assertFalse(parser.module(value))

    def test_unknown_fields_numeric_coercion_and_forged_loaded_claim_rejected(self):
        cases = []
        for field, value in (
            ("pid", True),
            ("observed_at", math.inf),
            ("send_returned", 1),
            ("sender", "com.example.Name"),
            ("schema", 1),
            ("phase", "native_call"),
        ):
            row = fixture()
            row[field] = value
            cases.append(row)
        row = fixture()
        row["extra"] = "private"
        cases.append(row)
        row = fixture()
        row["underlying_send"]["code"]["globals"] = {}
        cases.append(row)
        row = fixture()
        row["bus"]["mro_truncated"] = True
        cases.append(row)
        row = fixture()
        row["future"]["class"] = None
        cases.append(row)
        row = fixture()
        row["modules"].append(copy.deepcopy(row["modules"][0]))
        cases.append(row)
        row = fixture()
        row["modules_truncated"] = True
        cases.append(row)
        row = fixture()
        next(m for m in row["modules"] if "loaded_bytes_verified" in m)["loaded_bytes_verified"] = (
            True
        )
        cases.append(row)
        row = fixture()
        next(m for m in row["modules"] if "bytes" in m)["bytes"] = True
        cases.append(row)
        for i, row in enumerate(cases):
            with self.subTest(index=i):
                self.assertIsNone(parser.validate_provenance(row))

    def test_record_byte_and_total_file_limits(self):
        row = fixture()
        self.assertIsNone(parser.parse_provenance(parser.PREFIX + " " * 32769 + repr(row)))
        # Payload strips outer whitespace; use actual content to exercise the cap.
        self.assertIsNone(parser.parse_provenance(parser.PREFIX + repr({"huge": "x" * 32769})))
        row["underlying_send"]["qualname"] = "x" * 257
        self.assertIsNone(parser.validate_provenance(row))
        row = fixture()
        for module in row["modules"]:
            if module["status"] == "current_file_hashed":
                module["bytes"] = 4 * 1024 * 1024
        self.assertGreater(sum(m.get("bytes", 0) for m in row["modules"]), 8 * 1024 * 1024)
        self.assertIsNone(parser.validate_provenance(row))

    def test_duplicate_keys_and_nonliteral_rejected(self):
        value = repr(fixture())
        self.assertIsNone(parser.parse_provenance(parser.PREFIX + value[:-1] + ", 'pid': 22}"))
        self.assertIsNone(parser.parse_provenance(parser.PREFIX + "__import__('os').getpid()"))
        self.assertIsNone(parser.parse_provenance(parser.PREFIX + "\ud800"))

    def test_original_send_exception_has_no_future(self):
        row = fixture()
        row["send_returned"] = False
        old_modules = {item["module"]: item for item in row["modules"]}
        row["future"] = {"class": None, "mro": [], "mro_truncated": False}
        names = [row["underlying_send"]["module"]]
        for role in ("bus", "writer"):
            for item in row[role]["mro"]:
                if item["module"] and item["module"] not in names:
                    names.append(item["module"])
        row["modules"] = [old_modules[name] for name in names[:8]]
        row["modules_truncated"] = len(names) > 8
        self.assertEqual(parser.validate_provenance(row), row)


class ReadHelperTests(unittest.TestCase):
    def setUp(self):
        # unittest owns cleanup across setUp and all test failure paths.
        self.temp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.runtime, self.logs, self.proc = [base / name for name in ("runtime", "logs", "proc")]
        (self.runtime / "inverter_control").mkdir(parents=True)
        self.logs.mkdir()
        (self.proc / "22").mkdir(parents=True)
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        names = [
            "version",
            "main.py",
            "inverter_control/dbus_native.py",
            "inverter_control/victron.py",
        ] + [f"inverter_control/module_{i}.py" for i in range(27)]
        for name in names:
            (self.runtime / name).write_text(
                "v-test\n" if name == "version" else "# offline fixture\n"
            )
        self.hashes = {
            name: hashlib.sha256((self.runtime / name).read_bytes()).hexdigest() for name in names
        }
        self.boot = "12345678-1234-1234-1234-123456789abc"
        self.write_proc()
        self.expected = {
            "pid": 22,
            "sender": None,
            "version": "v-test",
            "runtime_hashes": self.hashes,
            "observed_monotonic_min": 90,
            "observed_monotonic_max": 110,
            "logged_realtime_min": 990,
            "logged_realtime_max": 1010,
            "process_start_ticks": 123,
            "boot_id": self.boot,
        }
        self.row = fixture()
        self.row.update(pid=22, loop_native_tid=23, sender=":1.42", observed_at=100)
        own = next(m for m in self.row["modules"] if m["module"] == "inverter_control.dbus_native")
        own.update(
            file=str(self.runtime / "inverter_control/dbus_native.py"),
            spec_origin=str(self.runtime / "inverter_control/dbus_native.py"),
            bytes=18,
            current_file_sha256=self.hashes["inverter_control/dbus_native.py"],
        )
        self.write_log()

    def write_proc(self, start=123):
        (self.proc / "22/stat").write_text(
            "22 (python) " + " ".join(["S"] + ["0"] * 18 + [str(start)]) + "\n"
        )
        (self.proc / "sys/kernel/random/boot_id").write_text(self.boot + "\n")

    def write_log(self, row=None, logged=1000):
        (self.logs / "current").write_text(
            stamp(logged) + " " + parser.PREFIX + repr(self.row if row is None else row) + "\n"
        )

    def collect(self, lookup=None):
        return reader.collect(
            self.runtime,
            self.logs,
            self.proc,
            self.expected,
            pid_lookup=lookup if lookup is not None else lambda _sender: 22,
        )

    def test_discovered_sender_is_independently_pid_checked(self):
        lookup = Mock(return_value=22)
        result = self.collect(lookup)
        self.assertEqual(result["status"], "matched_runtime_record")
        self.assertEqual(result["confirmed_sender"], ":1.42")
        lookup.assert_called_once_with(":1.42")
        self.assertFalse(
            next(m for m in result["record"]["modules"] if "loaded_bytes_verified" in m)[
                "loaded_bytes_verified"
            ]
        )

    def test_wrong_sender_owner_or_pinned_sender_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.collect(lambda _: 99)
        self.expected["sender"] = ":1.99"
        lookup = Mock()
        self.assertEqual(self.collect(lookup)["status"], "unavailable")
        lookup.assert_not_called()

    def test_disk_code_optional_and_no_loaded_memory_claim(self):
        self.row["underlying_send"]["code"] = {"status": "no_python_code"}
        self.write_log()
        result = self.collect()
        self.assertEqual(result["status"], "matched_runtime_record")
        self.assertEqual(result["record"]["underlying_send"]["code"]["status"], "no_python_code")
        self.assertIn("disk", result["limitations"][0])

    def test_stale_monotonic_or_wallclock_or_other_pid_are_unavailable(self):
        for kind in ("observed", "wall", "pid"):
            row = copy.deepcopy(self.row)
            if kind == "observed":
                row["observed_at"] = 80
            if kind == "pid":
                row["pid"] = 21
            self.write_log(row, logged=900 if kind == "wall" else 1000)
            lookup = Mock()
            with self.subTest(kind=kind):
                self.assertEqual(self.collect(lookup)["status"], "unavailable")
            lookup.assert_not_called()

    def test_two_distinct_records_ambiguous_duplicate_lines_deduplicate(self):
        path = self.logs / "current"
        original = path.read_text()
        path.write_text(original + original)
        self.assertEqual(self.collect()["status"], "matched_runtime_record")
        row = copy.deepcopy(self.row)
        row["observed_at"] += 1
        path.write_text(original + stamp() + " " + parser.PREFIX + repr(row) + "\n")
        lookup = Mock()
        self.assertEqual(self.collect(lookup)["status"], "ambiguous")
        lookup.assert_not_called()

    def test_runtime_inventory_hash_version_and_process_are_enforced(self):
        for field, bad in (
            ("version", "wrong"),
            ("process_start_ticks", 99),
            ("boot_id", "ffffffff-ffff-ffff-ffff-ffffffffffff"),
        ):
            old = self.expected[field]
            self.expected[field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.collect()
            self.expected[field] = old
        (self.runtime / "inverter_control/extra.py").write_text("# unexpected")
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.collect()
        (self.runtime / "inverter_control/extra.py").unlink()
        (self.runtime / "inverter_control/dbus_native.py").write_text("# changed")
        lookup = Mock()
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.collect(lookup)
        lookup.assert_not_called()

    def test_process_or_source_change_during_pid_lookup_rejected(self):
        def change(_sender):
            self.write_proc(start=124)
            return 22

        with self.assertRaisesRegex(ValueError, "Process start"):
            self.collect(change)

    def test_four_log_tails_bounded_and_symlink_rejected(self):
        line = (self.logs / "current").read_bytes()
        for i in range(5):
            path = self.logs / (stamp(990 + i) + ".s")
            path.write_bytes(b"x" * (1024 * 1024) + b"\n" + line)
        result = self.collect()
        self.assertEqual(result["status"], "matched_runtime_record")
        self.assertEqual(len(result["files"]), 4)
        self.assertLessEqual(sum(r["read_bytes"] for r in result["files"]), 4 * 1024 * 1024)
        (self.logs / (stamp(999) + ".s")).symlink_to(self.runtime / "version")
        with self.assertRaisesRegex(ValueError, "Non-regular log entry"):
            self.collect()

    def test_fifo_and_oversize_direct_reads_fail_without_blocking(self):
        fifo = self.logs / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "Non-regular"):
            reader.read_regular(fifo, 32)
        with self.assertRaisesRegex(ValueError, "byte limit"):
            reader.read_regular(self.logs / "current", 10)

    def test_pid_query_is_one_target_and_bounded_child_not_executed_in_test(self):
        with patch.object(
            reader.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=b"22\n"),
        ) as run:
            self.assertEqual(reader.lookup_sender_pid(":1.42"), 22)
            args, kwargs = run.call_args
            self.assertEqual(args[0][-1], ":1.42")
            self.assertIn("GetConnectionUnixProcessID", args[0][2])
            self.assertNotIn("ListNames", args[0][2])
            self.assertEqual(kwargs["timeout"], 2.0)
        with patch.object(
            reader.subprocess, "run", side_effect=subprocess.TimeoutExpired("probe", 2)
        ):
            with self.assertRaisesRegex(ValueError, "timed out"):
                reader.lookup_sender_pid(":1.42")
        with patch.object(reader.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                reader.lookup_sender_pid("bad;name")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

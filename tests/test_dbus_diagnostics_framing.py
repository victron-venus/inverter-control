"""Isolated diagnostic parser and reader regressions."""

import hashlib
import os
import unittest
from unittest.mock import Mock, patch

from scripts.dbus_diagnostics import provenance_parser as parser
from scripts.dbus_diagnostics import read_provenance as reader
from tests import test_dbus_diagnostics_provenance as fixtures


class FramingTests(fixtures.ReadHelperTests):
    def collect(self, lookup=None):
        return reader.collect(
            self.runtime,
            self.logs,
            self.proc,
            self.expected,
            pid_lookup=lookup if lookup is not None else lambda _sender: 22,
        )

    def split_log(self, split=None, end=True, next_name="current"):
        line = (self.logs / "current").read_bytes().removesuffix(b"\n")
        cut = split if split is not None else line.index(b"current_file_sha256") + 6
        archive = fixtures.stamp(1001) + ".s"
        (self.logs / "current").unlink()
        (self.logs / archive).write_bytes(line[:cut])
        (self.logs / next_name).write_bytes(line[cut:] + (b"\n" if end else b""))
        return line, archive

    def test_rotated_to_current_split_reassembled_exactly(self):
        line, archive = self.split_log()
        result = self.collect()
        self.assertEqual(result["status"], "matched_runtime_record")
        self.assertEqual(result["record"], self.row)
        self.assertEqual(result["log_framing"]["files"], [archive, "current"])
        self.assertEqual(
            result["log_framing"]["assembled_line_sha256"],
            hashlib.sha256(line).hexdigest(),
        )

    def test_rotated_to_rotated_split_uses_name_order_not_mtime(self):
        successor = fixtures.stamp(1002) + ".s"
        _, archive = self.split_log(next_name=successor)
        (self.logs / "current").write_bytes(b"ordinary log\n")
        os.utime(self.logs / archive, (3000, 3000))
        os.utime(self.logs / successor, (1000, 1000))
        result = self.collect()
        self.assertEqual(result["status"], "matched_runtime_record")
        self.assertEqual(result["log_framing"]["files"], [archive, successor])

    def test_unterminated_current_or_missing_successor_unavailable(self):
        self.split_log(end=False)
        lookup = Mock()
        self.assertEqual(self.collect(lookup)["status"], "unavailable")
        (self.logs / "current").unlink()
        self.assertEqual(self.collect(lookup)["status"], "unavailable")
        lookup.assert_not_called()

    def test_new_timestamp_and_known_middle_file_never_skipped(self):
        self.split_log()
        continuation = (self.logs / "current").read_bytes()
        (self.logs / (fixtures.stamp(1002) + ".s")).write_bytes(
            (fixtures.stamp(1002) + " ordinary line\n").encode()
        )
        self.assertEqual(self.collect()["status"], "unavailable")
        (self.logs / (fixtures.stamp(1002) + ".s")).unlink()
        (self.logs / "current").write_bytes((fixtures.stamp(1002) + " ").encode() + continuation)
        self.assertEqual(self.collect()["status"], "unavailable")

    def test_malformed_and_oversized_reassembly_rejected(self):
        self.split_log()
        path = self.logs / "current"
        continuation = path.read_bytes()
        path.write_bytes(b"INVALID" + continuation)
        self.assertEqual(self.collect()["status"], "unavailable")
        path.write_bytes(b"x" * (32769) + continuation)
        self.assertEqual(self.collect()["status"], "unavailable")

    def test_raw_payload_limit_applies_after_join_before_strip(self):
        line = (fixtures.stamp() + " " + parser.PREFIX + " " * 32769 + repr(self.row)).encode()
        (self.logs / (fixtures.stamp(1001) + ".s")).write_bytes(line[:1000])
        (self.logs / "current").write_bytes(line[1000:] + b"\n")
        self.assertEqual(self.collect()["status"], "unavailable")

    def test_prefix_may_split_before_phase_and_three_file_carry(self):
        line = (self.logs / "current").read_bytes().removesuffix(b"\n")
        a, b = fixtures.stamp(1001) + ".s", fixtures.stamp(1002) + ".s"
        (self.logs / a).write_bytes(line[:30])
        (self.logs / b).write_bytes(line[30:100])
        (self.logs / "current").write_bytes(line[100:] + b"\n")
        result = self.collect()
        self.assertEqual(result["status"], "matched_runtime_record")
        self.assertEqual(result["log_framing"]["files"], [a, b, "current"])

    def test_nonadjacent_or_dropped_prefix_segment_cannot_join(self):
        line = (self.logs / "current").read_bytes().removesuffix(b"\n")
        a, b = fixtures.stamp(1001) + ".s", fixtures.stamp(1002) + ".s"
        for ordered, offset in (([a, b, "current"], 0), ([a, "current"], 1)):
            output = reader.frame_segments(
                [
                    (a, line[:100], {"tail_offset": 0}),
                    ("current", line[100:] + b"\n", {"tail_offset": offset}),
                ],
                ordered,
            )
            self.assertTrue(all(parser.parse_provenance(raw.decode()) is None for raw, _ in output))

    def test_rotation_removal_or_replacement_during_read_rejected(self):
        _, archive = self.split_log()
        original = reader.read_regular

        def changed(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == "current":
                (self.logs / archive).unlink()
            return result

        with patch.object(reader, "read_regular", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "inventory changed"):
                self.collect()

    def test_ambiguous_rotation_name_pair_refused(self):
        _, archive = self.split_log()
        (self.logs / (archive[:-2] + ".u")).write_bytes(b"irrelevant")
        with self.assertRaisesRegex(ValueError, "ambiguous rotation"):
            self.collect()

    def test_four_log_tails_bounded_and_symlink_rejected(self):
        # The reader intentionally refuses a recognized symlink rather than skipping a
        # possibly intervening rotation. Source/proc behavior remains unchanged.
        line = (self.logs / "current").read_bytes()
        for i in range(5):
            path = self.logs / (fixtures.stamp(990 + i) + ".s")
            path.write_bytes(b"x" * (1024 * 1024) + b"\n" + line)
        result = self.collect()
        self.assertEqual(result["status"], "matched_runtime_record")
        self.assertEqual(len(result["files"]), 4)
        self.assertLessEqual(sum(r["read_bytes"] for r in result["files"]), 4 * 1024 * 1024)
        (self.logs / (fixtures.stamp(999) + ".s")).symlink_to(self.runtime / "version")
        with self.assertRaisesRegex(ValueError, "Non-regular log entry"):
            self.collect()

    def test_duplicate_assembled_and_complete_record_still_deduplicates(self):
        line, _ = self.split_log()
        with (self.logs / "current").open("ab") as f:
            f.write(line + b"\n")
        self.assertEqual(self.collect()["status"], "matched_runtime_record")

    def test_synthetic_fragments_preserve_exact_record_and_fingerprint(self):
        line = (self.logs / "current").read_bytes().removesuffix(b"\n")
        archive = fixtures.stamp(1001) + ".s"
        cut = line.index(b"current_file_sha256") + 6
        framed = reader.frame_segments(
            [
                (archive, line[:cut], {"tail_offset": 22434}),
                ("current", line[cut:] + b"\n", {"tail_offset": 0}),
            ],
            [archive, "current"],
        )
        self.assertEqual(len(framed), 1)
        row = parser.parse_provenance(framed[0][0].decode())
        self.assertEqual(row, self.row)
        self.assertEqual(
            row["underlying_send"]["code"]["sha256"], self.row["underlying_send"]["code"]["sha256"]
        )


if __name__ == "__main__":
    unittest.main()

"""Bounded adjacent-rotation framing for writer provenance records.

Requires explicit fresh identity, time-window and source-hash expectations.
No controller import, actuator call, controller signal, settings change or log write.
Only the selected D-Bus sender's PID is queried through a bounded owned child.
"""

# Exact wire schemas reject bools/subclasses; explicit guards keep rejection paths clear.
# pylint: disable=unidiomatic-typecheck,too-many-boolean-expressions

import argparse
import hashlib
import json
import os
import re
import stat

# Fixed interpreter argv; the child only queries one validated D-Bus sender.
import subprocess  # nosec B404
import sys
from pathlib import Path

from .provenance_parser import (
    PAYLOAD_LIMIT,
    digest,
    finite,
    parse_provenance,
    positive_int,
)

REQUIRED = {
    "pid",
    "sender",
    "version",
    "runtime_hashes",
    "observed_monotonic_min",
    "observed_monotonic_max",
    "logged_realtime_min",
    "logged_realtime_max",
    "process_start_ticks",
    "boot_id",
}
MAX_RUNTIME_FILES = 130  # 128 package entries plus main.py and version.
SOURCE_NAME = re.compile(r"(?:version|main\.py|inverter_control/[a-z_][a-z0-9_]*\.py)")
LOG_NAME = re.compile(r"(?:current|@[0-9a-f]{24}(?:\.[su])?)")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate expectation key")
        result[key] = value
    return result


def read_regular(path, limit, *, tail=False, growing=False):
    """Read at most limit bytes; no FIFO/device blocking and no final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Non-regular evidence file")
        if not tail and before.st_size > limit:
            raise ValueError("Evidence file exceeds byte limit")
        offset = max(0, before.st_size - limit) if tail else 0
        os.lseek(fd, offset, os.SEEK_SET)
        wanted = before.st_size - offset
        blocks, count = [], 0
        while count < wanted:
            block = os.read(fd, min(65536, wanted - count))
            if not block:
                break
            blocks.append(block)
            count += len(block)
        after = os.fstat(fd)
        if (
            count != wanted
            or after.st_size < before.st_size
            or (
                not growing
                and (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            )
        ):
            raise ValueError("Evidence file changed during read")
        data = b"".join(blocks)
        if offset:
            data = data.split(b"\n", 1)[1] if b"\n" in data else b""
        return data, {
            "size_before": before.st_size,
            "read_bytes": count,
            "tail_offset": offset,
            "device": before.st_dev,
            "inode": before.st_ino,
            "mtime_ns_before": before.st_mtime_ns,
        }
    finally:
        os.close(fd)


def validate_expectations(value):
    if type(value) is not dict or set(value) != REQUIRED:
        raise ValueError("Invalid expectation fields")
    if (
        not positive_int(value["pid"])
        or (
            value["sender"] is not None
            and (
                type(value["sender"]) is not str
                or re.fullmatch(r":[0-9]+\.[0-9]+", value["sender"]) is None
                or len(value["sender"]) > 128
            )
        )
        or type(value["version"]) is not str
        or re.fullmatch(r"[A-Za-z0-9.+-]{1,128}", value["version"]) is None
    ):
        raise ValueError("Invalid identity expectations")
    for prefix in ("observed_monotonic", "logged_realtime"):
        left, right = value[prefix + "_min"], value[prefix + "_max"]
        if not finite(left) or not finite(right) or right < left:
            raise ValueError("Invalid expected time window")
    hashes = value["runtime_hashes"]
    if (
        type(hashes) is not dict
        or not 4 <= len(hashes) <= MAX_RUNTIME_FILES
        or not {
            "version",
            "main.py",
            "inverter_control/dbus_native.py",
            "inverter_control/victron.py",
        }.issubset(hashes)
        or not all(
            type(name) is str and SOURCE_NAME.fullmatch(name) and digest(sha)
            for name, sha in hashes.items()
        )
    ):
        raise ValueError("Invalid runtime source expectations")
    if not positive_int(value["process_start_ticks"]):
        raise ValueError("Invalid process start expectation")
    if (
        type(value["boot_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["boot_id"]) is None
    ):
        raise ValueError("Invalid boot expectation")
    return value


def runtime_snapshot(root, proc_root, expected):
    version, budget = None, 8 * 1024 * 1024
    actual = {}
    names = {"main.py", "version"}
    with os.scandir(root / "inverter_control") as entries:
        for index, entry in enumerate(entries):
            if index >= 128:
                raise ValueError("Runtime directory entry limit exceeded")
            if entry.name.endswith(".py"):
                names.add("inverter_control/" + entry.name)
    if names != set(expected["runtime_hashes"]):
        raise ValueError("Runtime file inventory mismatch")
    for name, sha in sorted(expected["runtime_hashes"].items()):
        data, _ = read_regular(root / name, min(4 * 1024 * 1024, budget))
        budget -= len(data)
        actual[name] = hashlib.sha256(data).hexdigest()
        if actual[name] != sha:
            raise ValueError("Runtime source hash mismatch: " + name)
        if name == "version":
            if len(data) > 256:
                raise ValueError("Runtime version exceeds limit")
            version = data.decode("ascii").strip()
    if version != expected["version"]:
        raise ValueError("Runtime version mismatch")
    proc = read_proc(proc_root / str(expected["pid"]) / "stat", 4096)
    tail = proc[proc.rfind(b")") + 2 :].split()
    if proc.split(b" ", 1)[0] != str(expected["pid"]).encode() or len(tail) < 20:
        raise ValueError("Invalid process identity")
    start_ticks = int(tail[19])
    if not positive_int(start_ticks) or start_ticks != expected["process_start_ticks"]:
        raise ValueError("Process start identity mismatch")
    boot = read_proc(proc_root / "sys/kernel/random/boot_id", 64).decode("ascii").strip()
    if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot) is None:
        raise ValueError("Invalid current boot identity")
    if boot != expected["boot_id"]:
        raise ValueError("Boot identity mismatch")
    return {
        "pid": expected["pid"],
        "process_start_ticks": start_ticks,
        "boot_id": boot,
        "version": version,
        "runtime_hashes": actual,
    }


def read_proc(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Non-regular proc identity file")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError("Proc identity file exceeds limit")
        return data
    finally:
        os.close(fd)


def tai64n(line):
    token = line.split(" ", 1)[0]
    if re.fullmatch(r"@[0-9a-f]{24}", token) is None:
        return None
    nanos = int(token[17:25], 16)
    if nanos >= 1_000_000_000:
        return None
    return int(token[1:17], 16) - 0x400000000000000A + nanos / 1e9


PID_QUERY = """import dbus, sys
bus = dbus.SystemBus(private=True)
try:
    value = bus.call_blocking('org.freedesktop.DBus', '/org/freedesktop/DBus',
        'org.freedesktop.DBus', 'GetConnectionUnixProcessID', 's', (sys.argv[1],), timeout=2.0)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('invalid sender PID')
    print(int(value))
finally:
    bus.close()
"""


def lookup_sender_pid(sender):
    if (
        type(sender) is not str
        or re.fullmatch(r":[0-9]+\.[0-9]+", sender) is None
        or len(sender) > 128
    ):
        raise ValueError("Invalid sender for PID lookup")
    # A private, owned child bounds import/connect/query together. subprocess.run
    # kills and reaps only this child on timeout; no controller signal is sent.
    try:
        result = subprocess.run(  # nosec B603
            [sys.executable, "-c", PID_QUERY, sender],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("Sender PID lookup timed out") from error
    if result.returncode or re.fullmatch(rb"[0-9]{1,10}\n", result.stdout) is None:
        raise ValueError("Sender PID lookup failed")
    pid = int(result.stdout)
    if not positive_int(pid):
        raise ValueError("Sender PID lookup returned invalid identity")
    return pid


MAX_FRAMED_LINE = PAYLOAD_LIMIT + 512


def log_inventory(root):
    """Snapshot every eligible name, so adjacency never skips a known file."""
    result = {}
    rotations = set()
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 128:
                raise ValueError("Log directory entry limit exceeded")
            if not LOG_NAME.fullmatch(entry.name):
                continue
            # A recognized nonregular entry is a gap, not an ignorable neighbor.
            if not entry.is_file(follow_symlinks=False):
                raise ValueError("Non-regular log entry prevents continuity check")
            if entry.name != "current":
                token = entry.name[:25]
                if tai64n(token + " ") is None or token in rotations:
                    raise ValueError("Invalid or ambiguous rotation names")
                rotations.add(token)
            st = entry.stat(follow_symlinks=False)
            result[entry.name] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    return result


def chronological_names(inventory):
    return sorted(name for name in inventory if name != "current") + (
        ["current"] if "current" in inventory else []
    )


def frame_segments(segments, ordered_names):
    """Assemble only an unterminated tail into its observed adjacent prefix.

    segments contain bounded file bytes and full-file tail offsets; no arbitrary
    search for a later completion and no synthetic separators are permitted.
    """
    lines = []
    carry, origins, previous = b"", [], None
    positions = {name: index for index, name in enumerate(ordered_names)}
    for name, data, info in segments:
        if name not in positions or len(data) > 1024 * 1024:
            raise ValueError("Invalid bounded log segment")
        parts = data.split(b"\n")
        if carry:
            contiguous = (
                previous != "current"
                and positions[name] == positions[previous] + 1
                and info["tail_offset"] == 0
                and not parts[0].startswith(b"@")
            )
            if contiguous and len(carry) + len(parts[0]) <= MAX_FRAMED_LINE:
                parts[0] = carry + parts[0]
                first_origins = origins + [name]
            else:
                first_origins = [name]
            carry, origins = b"", []
        else:
            first_origins = [name]
        for index, raw in enumerate(parts[:-1]):
            lines.append((raw, first_origins if index == 0 else [name]))
        final = parts[-1]
        # An unfinished current line stays unfinished; it never becomes evidence.
        if (
            name != "current"
            and final
            and len(final) <= MAX_FRAMED_LINE
            and final.startswith(b"@")
            and tai64n(final.decode("utf-8", errors="replace")) is not None
        ):
            carry = final
            origins = first_origins if len(parts) == 1 else [name]
        previous = name
    return lines


def read_log_lines(root):
    before = log_inventory(root)
    ordered = chronological_names(before)
    chosen = ordered[-4:]
    segments, files = [], []
    for name in chosen:
        data, info = read_regular(root / name, 1024 * 1024, tail=True, growing=name == "current")
        expected = before[name]
        actual = (
            info["device"],
            info["inode"],
            info["size_before"],
            info["mtime_ns_before"],
        )
        if actual[:2] != expected[:2] or (
            (name != "current" and actual != expected)
            or (name == "current" and actual[2] < expected[2])
        ):
            raise ValueError("Log identity changed before read")
        segments.append((name, data, info))
        files.append({"name": name, **info})
    after = log_inventory(root)
    if set(after) != set(before):
        raise ValueError("Log inventory changed during read")
    for name, expected in before.items():
        actual = after[name]
        if actual[:2] != expected[:2] or (
            (name != "current" and actual != expected)
            or (name == "current" and actual[2] < expected[2])
        ):
            raise ValueError("Log identity changed during read")
    for name, _data, info in segments:
        if after[name][2] < info["size_before"]:
            raise ValueError("Log truncated after read")
    return frame_segments(segments, ordered), files


def collect(runtime_root, log_root, proc_root, expected, *, pid_lookup=lookup_sender_pid):
    expected = validate_expectations(expected)
    before = runtime_snapshot(runtime_root, proc_root, expected)
    lines, files = read_log_lines(log_root)
    records = {}
    rejected, outside, mismatched, seen = 0, 0, 0, 0
    for raw, fragments in lines:
        if b"writer_provenance" not in raw:
            continue
        seen += 1
        if seen > 128:
            raise ValueError("Provenance candidate limit exceeded")
        try:
            line = raw.decode("utf-8")
        except UnicodeError:
            rejected += 1
            continue
        logged, row = tai64n(line), parse_provenance(line)
        if logged is None or row is None or len(raw) > MAX_FRAMED_LINE:
            rejected += 1
            continue
        if row["pid"] != expected["pid"] or (
            expected["sender"] is not None and row["sender"] != expected["sender"]
        ):
            mismatched += 1
            continue
        if not (
            expected["observed_monotonic_min"]
            <= row["observed_at"]
            <= expected["observed_monotonic_max"]
            and expected["logged_realtime_min"] <= logged <= expected["logged_realtime_max"]
        ):
            outside += 1
            continue
        identity = json.dumps(row, sort_keys=True, allow_nan=False)
        records.setdefault(
            identity,
            {
                "record": row,
                "logged_at_unix": logged,
                "log_framing": {
                    "files": fragments,
                    "joined_rotation_boundary": len(fragments) > 1,
                    "assembled_line_bytes": len(raw),
                    "assembled_line_sha256": hashlib.sha256(raw).hexdigest(),
                    "continuity": "Adjacent names in stable observed full directory inventory; no earlier deletion history is proved.",
                },
            },
        )
    result = {
        "status": "unavailable",
        "runtime_assertions": "passed_current_disk_and_process_snapshot",
        "identity": before,
        "files": files,
        "valid_distinct_records": len(records),
        "rejected_candidates": rejected,
        "outside_expected_window": outside,
        "other_pid_or_sender": mismatched,
        "record": None,
        "limitations": [
            "Current runtime and module file hashes are disk evidence, not proof of loaded compiled bytes.",
            "Rotation adjacency describes the stable observed inventory only; files deleted before it are not reconstructible. New timestamp, skipped known file, dropped prefix or missing newline prevents joining.",
            "Captured Python code fingerprint is optional and not a complete behavior or machine-code proof.",
            "One first-send record may be dropped by runtime queue contention/bounds; absence is unavailable evidence.",
            "Byte/count limits do not impose a filesystem I/O wall-clock deadline.",
            "Process/boot before-and-after checks cover this read; record identity also relies on the supplied fresh wall-clock and monotonic windows.",
        ],
    }
    if len(records) > 1:
        result["status"] = "ambiguous"
    elif len(records) == 1:
        match = next(iter(records.values()))
        own = [
            m for m in match["record"]["modules"] if m["module"] == "inverter_control.dbus_native"
        ]
        if (
            len(own) != 1
            or own[0]["status"] != "current_file_hashed"
            or own[0]["file"] != str(runtime_root / "inverter_control/dbus_native.py")
            or own[0]["current_file_sha256"]
            != expected["runtime_hashes"]["inverter_control/dbus_native.py"]
        ):
            result["status"] = "record_runtime_module_mismatch"
        else:
            sender_pid = pid_lookup(match["record"]["sender"])
            if not positive_int(sender_pid) or sender_pid != expected["pid"]:
                raise ValueError("D-Bus sender does not belong to expected process")
            result.update(
                status="matched_runtime_record",
                confirmed_sender=match["record"]["sender"],
                sender_owner_pid=sender_pid,
                **match,
            )
    after = runtime_snapshot(runtime_root, proc_root, expected)
    if before != after:
        raise ValueError("Runtime/process identity changed during read")
    return result


def read_expectations(stream):
    """Read one bounded JSON document from the caller-owned input stream."""
    data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Expectations exceed byte limit")
    return validate_expectations(json.loads(data, object_pairs_hook=unique_object))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.epilog = "Read the expected identity and source hashes as JSON from standard input."
    parser.add_argument("--runtime-root", type=Path, default=Path("/data/inverter-control"))
    parser.add_argument("--log-root", type=Path, default=Path("/var/log/inverter-control"))
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    args = parser.parse_args()
    try:
        expected = read_expectations(sys.stdin.buffer)
        result = collect(args.runtime_root, args.log_root, args.proc_root, expected)
    except (ValueError, OSError, UnicodeError, KeyError, TypeError) as error:
        result = {
            "status": "rejected",
            "error_type": type(error).__name__,
            "reason": str(error)
            if type(error) is ValueError
            else "Evidence unavailable or malformed",
        }
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "matched_runtime_record" else 1


if __name__ == "__main__":
    raise SystemExit(main())

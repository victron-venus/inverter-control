"""Current producer contracts, exercised without a D-Bus connection or device."""

import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from inverter_control import dbus_native as native
from scripts.dbus_diagnostics import provenance_parser, read_provenance, send_timing_parser
from tests import test_dbus_diagnostics_provenance as helpers
from tests.test_dbus_diagnostics_timing import record
from tests.test_dbus_send_provenance import captured_refs


def test_current_producer_provenance_roundtrips():
    """Serialize actual captured writer references, not a manually copied schema."""
    produced = native._serialize_send_provenance(captured_refs())
    assert provenance_parser.validate_provenance(produced) == produced
    assert provenance_parser.parse_provenance(provenance_parser.PREFIX + repr(produced)) == produced


@pytest.mark.parametrize("timeout", [False, True])
def test_current_producer_timing_roundtrips(timeout):
    timing = record()["anchors"]
    timing.update(caller_native_tid=22, loop_native_tid=23, send_future_cancelled=False)
    if timeout:
        timing.update(returned_at=100.3, reply_observed_at=None, send_done_observed_at=None)
        timing["send_future_cancelled"] = None
    client = native.NativeDbusClient(observe_write_send=True)
    client._record_write_timing(
        timing, SimpleNamespace(unique_name=":1.42"), SimpleNamespace(serial=9)
    )
    produced = client._write_timings.pop()
    assert send_timing_parser.validate_timing(produced) == produced
    assert send_timing_parser.parse_timing(send_timing_parser.PREFIX + repr(produced)) == produced


@pytest.fixture(name="runtime_fixture")
def isolated_runtime():
    """Reuse setup without collecting the inherited unittest cases again."""
    case = helpers.ReadHelperTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def test_current_repository_runtime_inventory(runtime_fixture):
    root = Path(__file__).resolve().parents[1]
    for old in (runtime_fixture.runtime / "inverter_control").glob("*.py"):
        old.unlink()
    sources = [root / "main.py", root / "version"] + sorted(
        (root / "inverter_control").glob("*.py")
    )
    runtime_fixture.hashes.clear()
    for source in sources:
        relative = source.relative_to(root).as_posix()
        content = source.read_bytes()
        (runtime_fixture.runtime / relative).write_bytes(content)
        runtime_fixture.hashes[relative] = hashlib.sha256(content).hexdigest()
    runtime_fixture.expected["version"] = (root / "version").read_text().strip()
    read_provenance.validate_expectations(runtime_fixture.expected)
    snapshot = read_provenance.runtime_snapshot(
        runtime_fixture.runtime, runtime_fixture.proc, runtime_fixture.expected
    )
    assert snapshot["runtime_hashes"] == runtime_fixture.hashes


def test_bounded_inventory_and_required_entrypoints(runtime_fixture):
    for count in (0, read_provenance.MAX_RUNTIME_FILES + 1):
        expected = copy.deepcopy(runtime_fixture.expected)
        expected["runtime_hashes"] = {
            f"inverter_control/module_{i}.py": "0" * 64 for i in range(count)
        }
        with pytest.raises(ValueError, match="source expectations"):
            read_provenance.validate_expectations(expected)
    expected = copy.deepcopy(runtime_fixture.expected)
    del expected["runtime_hashes"]["main.py"]
    with pytest.raises(ValueError, match="source expectations"):
        read_provenance.validate_expectations(expected)


def test_valid_changed_count_still_requires_exact_on_disk_names(runtime_fixture):
    extra = runtime_fixture.runtime / "inverter_control/extra.py"
    extra.write_text("# newly installed source\n")
    with pytest.raises(ValueError, match="inventory mismatch"):
        read_provenance.runtime_snapshot(
            runtime_fixture.runtime, runtime_fixture.proc, runtime_fixture.expected
        )
    runtime_fixture.hashes["inverter_control/extra.py"] = hashlib.sha256(
        extra.read_bytes()
    ).hexdigest()
    read_provenance.validate_expectations(runtime_fixture.expected)
    assert read_provenance.runtime_snapshot(
        runtime_fixture.runtime, runtime_fixture.proc, runtime_fixture.expected
    )


def test_optional_text_requires_explicit_null_or_plain_string():
    assert provenance_parser.text(None, 4)
    assert not provenance_parser.text(None, 4, optional=False)
    assert provenance_parser.text("name", 4)
    assert not provenance_parser.text("longer", 4)
    for value in (False, 123, {}, []):
        assert not provenance_parser.text(value, 4)


def test_expectations_input_is_bounded_and_rejects_duplicate_keys(runtime_fixture):
    data = json.dumps(runtime_fixture.expected).encode()
    assert read_provenance.read_expectations(io.BytesIO(data)) == runtime_fixture.expected
    for invalid, message in (
        (b" " * 65537, "byte limit"),
        (b'{"pid": 22, "pid": 23}', "Duplicate expectation key"),
        (b"{}", "Invalid expectation fields"),
    ):
        with pytest.raises(ValueError, match=message):
            read_provenance.read_expectations(io.BytesIO(invalid))


def test_reader_cli_uses_caller_supplied_stdin(runtime_fixture, monkeypatch, capsys):
    data = json.dumps(runtime_fixture.expected).encode()
    monkeypatch.setattr(read_provenance.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
    monkeypatch.setattr(read_provenance.sys, "argv", ["read_provenance"])
    collect = Mock(return_value={"status": "matched_runtime_record"})
    monkeypatch.setattr(read_provenance, "collect", collect)
    assert read_provenance.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "matched_runtime_record"
    assert collect.call_args.args[-1] == runtime_fixture.expected


def test_reader_cli_rejects_invalid_input_before_reading_runtime(monkeypatch, capsys):
    monkeypatch.setattr(read_provenance.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(read_provenance.sys, "argv", ["read_provenance"])
    collect = Mock()
    monkeypatch.setattr(read_provenance, "collect", collect)
    assert read_provenance.main() == 1
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
    collect.assert_not_called()

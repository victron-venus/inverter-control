"""An ambiguous enqueue is never replayed or described as confirmed."""

import concurrent.futures
import io
import json
import os
import stat

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
import sys
import textwrap
import time
from http.client import HTTPMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_precharge_notification import request

from inverter_control.mqtt_bridge import MQTTBridge
from inverter_control.precharge import PrechargeInbox
from inverter_control.webhook_server import WebhookHandler


def test_enqueue_failure_is_sticky_across_http_mqtt_and_restart(tmp_path):
    path = tmp_path / "requests.json"
    inbox = PrechargeInbox(path)
    accept = Mock(side_effect=RuntimeError("enqueue failed"))
    payload = request()
    raw = json.dumps(payload).encode()
    responses = []
    callback = lambda p: inbox.handle(p, lambda: False, accept)
    headers = HTTPMessage()
    headers["Content-Length"] = str(len(raw))
    handler = SimpleNamespace(
        headers=headers,
        rfile=io.BytesIO(raw),
        pre_charge_callback=callback,
        _send_response=lambda status, body: responses.append((status, body)),
    )
    WebhookHandler._handle_pre_charge(handler)
    assert responses[0][0] == 503
    assert responses[0][1]["reason"] == "decision_unavailable"

    inbox = PrechargeInbox(path)
    bridge = SimpleNamespace(
        forecast_prefix="synthetic/site",
        _callbacks={"pre_charge": callback},
        _disconnect_requested=False,
    )
    client = Mock()
    message = SimpleNamespace(topic="synthetic/site/pre_charge_request", payload=raw, retain=False)
    MQTTBridge._on_message(bridge, client, None, message)
    ack = json.loads(client.publish.call_args.args[1])
    assert (ack["status"], ack["http_status"], ack["reason"]) == (
        "unavailable",
        503,
        "decision_uncertain",
    )
    assert ack["request_id"] == payload["request_id"]
    assert "original_status" not in ack
    assert client.publish.call_args.kwargs == {"qos": 1, "retain": False}
    handler.rfile = io.BytesIO(raw)
    WebhookHandler._handle_pre_charge(handler)
    assert responses[1][0] == 503
    assert responses[1][1]["reason"] == "decision_uncertain"
    accept.assert_called_once()


@pytest.mark.parametrize("phase", ["reservation", "confirmation"])
@pytest.mark.parametrize(
    "boundary", ["file_fsync", "before_replace", "after_replace", "directory_fsync", "after_save"]
)
def test_persistence_failure_never_repeats_callback(tmp_path, monkeypatch, phase, boundary):
    path = tmp_path / "requests.json"
    inbox = PrechargeInbox(path)
    payload = request()
    accept = Mock()
    original_save = inbox._save
    original_replace = os.replace
    original_fsync = os.fsync
    saves = 0
    selected_save = 1 if phase == "reservation" else 2

    def save(records):
        nonlocal saves
        saves += 1
        original_save(records)
        if saves == selected_save and boundary == "after_save":
            raise OSError("save returned before injected fault")

    def replace(src, dst):
        selected = saves == selected_save
        if selected and boundary == "before_replace":
            raise OSError("replace not attempted")
        original_replace(src, dst)
        if selected and boundary == "after_replace":
            raise OSError("replace completed before injected fault")

    def fsync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if saves == selected_save and (
            (boundary == "directory_fsync" and is_directory)
            or (boundary == "file_fsync" and not is_directory)
        ):
            raise OSError("fsync failed")
        original_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(inbox, "_save", save)
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "fsync", fsync)
        first = inbox.handle(payload, lambda: False, accept)
    expected_calls = 0 if phase == "reservation" else 1
    assert (first["status"], first["http_status"]) == ("unavailable", 503)
    assert accept.call_count == expected_calls
    assert inbox.records[payload["request_id"]]["status"] == "uncertain"
    retry = inbox.handle(payload, lambda: False, accept)
    assert (retry["http_status"], retry["reason"]) == (503, "decision_uncertain")
    assert accept.call_count == expected_calls

    restarted = PrechargeInbox(path)
    result = restarted.handle(payload, lambda: False, accept)
    if phase == "reservation" and boundary in {"file_fsync", "before_replace"}:
        # Nothing was published or enqueued. A fresh process can reserve safely.
        assert result["status"] == "accepted"
    elif phase == "confirmation" and boundary not in {"file_fsync", "before_replace"}:
        # The completed callback's marker reached disk. No replay is needed.
        assert (result["status"], result["original_status"]) == ("duplicate", "accepted")
    else:
        assert (result["http_status"], result["reason"]) == (503, "decision_uncertain")
    assert accept.call_count <= 1


@pytest.mark.parametrize(
    "crash_at,callback_ran", [("reserved", False), ("callback_returned", True)]
)
def test_process_exit_leaves_unavailable_reservation(tmp_path, crash_at, callback_ran):
    payload = request()
    payload_path = tmp_path / "request.json"
    payload_path.write_text(json.dumps(payload))
    child = textwrap.dedent("""
        import json
        import os
        from pathlib import Path
        import sys
        from inverter_control.precharge import PrechargeInbox

        target = Path(sys.argv[1])
        phase = sys.argv[2]
        inbox = PrechargeInbox(target / "journal.json")
        save = inbox._save
        calls = 0

        def crash_save(records):
            global calls
            calls += 1
            if phase == "callback_returned" and calls == 2:
                os._exit(86)
            save(records)
            if phase == "reserved" and calls == 1:
                os._exit(86)

        inbox._save = crash_save
        payload = json.loads((target / "request.json").read_text())
        inbox.handle(payload, lambda: False, lambda: (target / "called").touch())
        raise AssertionError("injected exit was not reached")
    """)
    # Isolated test fixture; explicit argv, never shell interpolation.
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", child, str(tmp_path), crash_at],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 86, result.stderr.decode()
    assert (tmp_path / "called").exists() is callback_ran
    accept = Mock()
    retry = PrechargeInbox(tmp_path / "journal.json").handle(payload, lambda: False, accept)
    assert (retry["http_status"], retry["reason"]) == (503, "decision_uncertain")
    accept.assert_not_called()


@pytest.mark.parametrize(
    "record,expected_status,expected_reason",
    [
        ({"status": "accepted"}, "unavailable", "legacy_decision_unverified"),
        ({"status": "accepted", "queued": True}, "duplicate", "already_decided"),
        ({"status": "suppressed"}, "duplicate", "already_decided"),
        ({"status": "uncertain"}, "unavailable", "decision_uncertain"),
    ],
)
def test_migration_never_upgrades_legacy_evidence(
    tmp_path, record, expected_status, expected_reason
):
    path = tmp_path / "requests.json"
    payload = request()
    original = json.dumps({payload["request_id"]: {**record, "until": time.time() + 172800}})
    path.write_text(original)
    accept = Mock()
    result = PrechargeInbox(path).handle(payload, lambda: False, accept)
    assert (result["status"], result["reason"]) == (expected_status, expected_reason)
    accept.assert_not_called()
    assert path.read_text() == original


@pytest.mark.parametrize(
    "record",
    [
        {"status": "accepted", "queued": False},
        {"status": "accepted", "queued": 1},
        {"status": "uncertain", "queued": True},
        {"status": "suppressed", "queued": True},
    ],
)
def test_invalid_confirmation_marker_fails_closed(tmp_path, record):
    path = tmp_path / "requests.json"
    payload = request()
    path.write_text(json.dumps({payload["request_id"]: {**record, "until": time.time() + 300}}))
    accept = Mock()
    result = PrechargeInbox(path).handle(payload, lambda: False, accept)
    assert result["reason"] == "journal_unavailable"
    accept.assert_not_called()


def test_concurrent_retries_do_not_repeat_failed_enqueue(tmp_path):
    inbox = PrechargeInbox(tmp_path / "requests.json")
    accept = Mock(side_effect=RuntimeError("enqueue failed"))
    payload = request()
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: inbox.handle(payload, lambda: False, accept), range(16)))
    assert all(result["http_status"] == 503 for result in results)
    assert [result["reason"] for result in results].count("decision_unavailable") == 1
    assert [result["reason"] for result in results].count("decision_uncertain") == 15
    accept.assert_called_once()


def test_partial_callback_failure_remains_uncertain_without_replay(tmp_path):
    path = tmp_path / "requests.json"
    queued = []

    def accept():
        queued.append("synthetic intent")
        raise RuntimeError("callback failed after a side effect")

    payload = request()
    result = PrechargeInbox(path).handle(payload, lambda: False, accept)
    assert result["http_status"] == 503
    retry = PrechargeInbox(path).handle(payload, lambda: False, accept)
    assert (retry["http_status"], retry["reason"]) == (503, "decision_uncertain")
    assert queued == ["synthetic intent"]


def test_uncertain_retention_expires_without_replaying_old_payload(tmp_path):
    path = tmp_path / "requests.json"
    payload = request()
    path.write_text(json.dumps({payload["request_id"]: {"status": "uncertain", "until": 0}}))
    inbox = PrechargeInbox(path)
    accept = Mock()
    assert inbox.handle({**payload, "expires_at": 0}, lambda: False, accept)["http_status"] == 410
    accept.assert_not_called()
    assert inbox.handle(payload, lambda: False, accept)["http_status"] == 202
    accept.assert_called_once()
    assert inbox.records[payload["request_id"]]["queued"] is True

"""Regression coverage for configuration loss, failed pushes and multilog rotation."""

import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from inverter_control import log_forwarder as forwarder

REPO = Path(__file__).resolve().parents[1]


def archive_path(directory, sequence):
    return directory / f"@{sequence:024x}.s"


@pytest.fixture
def source(tmp_path, monkeypatch):
    current = tmp_path / "current"
    state = tmp_path / "state.json"
    monkeypatch.setattr(forwarder, "LOG_SOURCES", {"test": str(current)})
    monkeypatch.setattr(forwarder, "STATE_FILE", str(state))
    return current, state


def test_failed_batch_is_retried_after_multiple_rotations(source, monkeypatch):
    current, _ = source
    expected = [f"old {number}" for number in range(205)]
    current.write_text("\n".join(expected) + "\n")
    accepted = []
    attempts = []

    def push(payload):
        lines = [value[1] for value in payload["streams"][0]["values"]]
        attempts.append(lines)
        if len(attempts) == 2:
            return False
        accepted.extend(lines)
        return True

    monkeypatch.setattr(forwarder, "push_to_loki", push)
    forwarder.process_logs()  # acknowledge the first 100 lines
    checkpoint = forwarder.load_state()
    forwarder.process_logs()  # outage: the next 100 stay pending
    assert forwarder.load_state() == checkpoint

    current.rename(archive_path(current.parent, 1))
    archive_path(current.parent, 2).write_text("second archive\n")
    current.write_text("new current\n")
    for _ in range(6):
        forwarder.process_logs()

    assert attempts[1] == attempts[2]
    assert accepted == expected + ["second archive", "new current"]
    assert forwarder.load_state()["test"]["inode"] == current.stat().st_ino


def test_first_failed_push_survives_rotation_before_any_checkpoint(source, monkeypatch):
    current, state = source
    current.write_text("pending\n")
    monkeypatch.setattr(forwarder, "push_to_loki", lambda payload: False)
    forwarder.process_logs()
    assert not state.exists()
    current.rename(archive_path(current.parent, 1))
    current.write_text("next\n")
    accepted = []

    def accept(payload):
        accepted.extend(value[1] for value in payload["streams"][0]["values"])
        return True

    monkeypatch.setattr(forwarder, "push_to_loki", accept)
    forwarder.process_logs()
    forwarder.process_logs()
    assert accepted == ["pending", "next"]


def test_rotated_log_is_read_during_gap_before_current_exists(source, monkeypatch):
    current, _ = source
    current.write_text("before\nremaining\n")
    forwarder.save_state({"test": {"position": len("before\n"), "inode": current.stat().st_ino}})
    current.rename(archive_path(current.parent, 1))
    accepted = []

    def accept(payload):
        accepted.extend(value[1] for value in payload["streams"][0]["values"])
        return True

    monkeypatch.setattr(forwarder, "push_to_loki", accept)
    forwarder.process_logs()
    assert accepted == ["remaining"]


def test_rotation_during_directory_scan_keeps_unread_old_inode(source, monkeypatch, capsys):
    current, _ = source
    current.write_text("acknowledged\nunread old line\n")
    position, inode = len("acknowledged\n"), current.stat().st_ino
    real_listdir = os.listdir
    rotated = False

    def listdir_then_rotate(directory):
        nonlocal rotated
        names = real_listdir(directory)
        if not rotated:
            current.rename(archive_path(current.parent, 1))
            current.write_text("new line\n")
            rotated = True
        return names

    monkeypatch.setattr(forwarder.os, "listdir", listdir_then_rotate)
    assert forwarder.read_new_lines(str(current), position, inode) == ([], position, inode)
    lines, position, inode = forwarder.read_new_lines(str(current), position, inode)
    assert lines == ["unread old line"]
    assert forwarder.read_new_lines(str(current), position, inode)[0] == ["new line"]
    assert "Retention loss" not in capsys.readouterr().err


def test_retention_loss_is_reported_and_available_logs_are_recovered(source, capsys):
    current, _ = source
    current.write_text("new\n")
    oldest = archive_path(current.parent, 2)
    oldest.write_text("still retained\n")
    missing_inode = max(current.stat().st_ino, oldest.stat().st_ino) + 1
    lines, _, inode = forwarder.read_new_lines(str(current), 9000, missing_inode)
    assert lines == ["still retained"]
    assert inode == oldest.stat().st_ino
    assert "Retention loss" in capsys.readouterr().err


def test_finished_archives_do_not_replay_or_block_current(source):
    current, _ = source
    oldest = archive_path(current.parent, 1)
    oldest.write_text("already acknowledged\n")
    archive_path(current.parent, 2).touch()
    (current.parent / "previous").touch()
    current.write_text("current entry\n")
    lines, position, inode = forwarder.read_new_lines(
        str(current), oldest.stat().st_size, oldest.stat().st_ino
    )
    assert lines == ["current entry"]
    assert position == current.stat().st_size
    assert inode == current.stat().st_ino


def test_partial_utf8_line_is_not_acknowledged_until_completed(source):
    current, _ = source
    current.write_bytes(b"complete\npartial \xe2\x82")
    lines, position, inode = forwarder.read_new_lines(str(current), 0, None)
    assert lines == ["complete"]
    assert position == len(b"complete\n")
    assert forwarder.read_new_lines(str(current), position, inode) == ([], position, inode)
    with current.open("ab") as stream:
        stream.write(b"\xac\n")
    assert forwarder.read_new_lines(str(current), position, inode)[0] == ["partial €"]


def test_blank_lines_advance_cursor(source, monkeypatch):
    current, _ = source
    current.write_text("\n\r\n")
    monkeypatch.setattr(
        forwarder, "push_to_loki", lambda payload: pytest.fail("No log payload expected")
    )
    forwarder.process_logs()
    assert forwarder.load_state()["test"]["position"] == current.stat().st_size


def test_truncation_restarts_with_warning(source, capsys):
    current, _ = source
    current.write_text("new\n")
    assert forwarder.read_new_lines(str(current), 500, current.stat().st_ino)[0] == ["new"]
    assert "Log truncated" in capsys.readouterr().err


def test_atomic_state_failure_keeps_last_acknowledged_cursor(source, monkeypatch):
    _, state = source
    checkpoint = {"test": {"position": 42, "inode": 5}}
    forwarder.save_state(checkpoint)

    def fail_replace(*_):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(forwarder.os, "replace", fail_replace)
    forwarder.save_state({"test": {"position": 84, "inode": 5}})
    assert json.loads(state.read_text()) == checkpoint
    assert list(state.parent.glob(".log-forwarder-*")) == []


@pytest.mark.parametrize("broken", [[], None, {"test": []}, {"test": {"position": "wrong"}}])
def test_corrupt_state_does_not_break_forwarding(source, broken):
    _, state = source
    state.write_text(json.dumps(broken))
    assert forwarder.load_state() == {}


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://example.com:bad/push",
        "https://example.com/push\nvalue",
        "https://example.com/push#fragment",
        "http://example.com/push",
    ],
)
def test_endpoint_file_validation(tmp_path, monkeypatch, url):
    config = tmp_path / "loki_url"
    config.write_text(url)
    monkeypatch.delenv("LOKI_URL", raising=False)
    monkeypatch.setenv("LOKI_URL_FILE", str(config))
    with pytest.raises(ValueError):
        forwarder.load_loki_url()


def test_missing_endpoint_has_no_implicit_network_destination(tmp_path, monkeypatch):
    monkeypatch.delenv("LOKI_URL", raising=False)
    monkeypatch.setenv("LOKI_URL_FILE", str(tmp_path / "missing"))
    assert forwarder.load_loki_url() == ""


@pytest.fixture
def loki_server():
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/loki/api/v1/push", payloads
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def wait_until(condition, process, output):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        assert process.poll() is None, output.read_text()
        if condition():
            return
        time.sleep(0.01)
    pytest.fail(f"Forwarder did not reach expected state: {output.read_text()}")


@pytest.mark.parametrize("configure_at_start", [True, False])
def test_service_loads_persistent_endpoint_and_recovers_invalid_config(
    tmp_path, loki_server, configure_at_start
):
    endpoint, payloads = loki_server
    config = tmp_path / "setupOptions/inverter-control/loki_url"
    config.parent.mkdir(parents=True)
    if configure_at_start:
        config.write_text(endpoint + "\n")
    current = tmp_path / "current"
    current.write_text("service startup log\n")
    install = tmp_path / "inverter-control"
    (install / "inverter_control").mkdir(parents=True)
    # Run the shipped service launcher and real main loop with isolated log paths
    # and shortened intervals. There is no network access outside this local server.
    (install / "inverter_control/log_forwarder.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from inverter_control import log_forwarder as f\n"
        f"f.LOG_SOURCES = {{'test': {str(current)!r}}}\n"
        "f.CONFIG_RETRY_INTERVAL = f.POLL_INTERVAL = 0.02\n"
        "f.main()\n"
    )
    launcher = tmp_path / "run"
    launcher.write_text(
        (REPO / "service/log-forwarder/run")
        .read_text()
        .replace("/data/inverter-control", str(install))
    )
    env = {key: value for key, value in os.environ.items() if key != "LOKI_URL"}
    env.update(LOKI_URL_FILE=str(config), STATE_FILE=str(tmp_path / "state.json"))
    env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
    output = tmp_path / "service-output"
    with output.open("w") as logs:
        process = subprocess.Popen(["sh", str(launcher)], stdout=logs, stderr=logs, env=env)
        try:
            if not configure_at_start:
                wait_until(lambda: "not configured" in output.read_text(), process, output)
                assert payloads == []
                config.write_text("not a URL\n")
                wait_until(lambda: "unreadable or invalid" in output.read_text(), process, output)
                assert payloads == []
                config.write_text(endpoint + "\n")
            wait_until(lambda: bool(payloads), process, output)
            assert payloads[0]["streams"][0]["values"][0][1] == "service startup log"
        finally:
            process.terminate()
            process.wait(timeout=3)


def test_environment_endpoint_overrides_persistent_configuration(tmp_path, monkeypatch):
    config = tmp_path / "loki_url"
    config.write_text("not valid")
    monkeypatch.setenv("LOKI_URL_FILE", str(config))
    monkeypatch.setenv("LOKI_URL", "https://example.com/loki/api/v1/push")
    assert forwarder.load_loki_url() == "https://example.com/loki/api/v1/push"
    monkeypatch.setenv("LOKI_URL", "")
    assert forwarder.load_loki_url() == ""


def test_staged_upgrade_preserves_device_loki_configuration(tmp_path):
    import shutil

    from test_installer import fake_device

    package, _, env = fake_device(tmp_path)
    config = package.parent / "setupOptions/inverter-control/loki_url"
    config.parent.mkdir(parents=True)
    config.write_text("https://example.com/loki/api/v1/push\n")
    config.chmod(0o600)
    original = config.stat()
    stage = tmp_path / "release"
    shutil.copytree(package, stage)
    subprocess.run(["sh", str(stage / "update.sh"), str(package)], cwd=stage, env=env, check=True)
    assert config.read_text() == "https://example.com/loki/api/v1/push\n"
    assert config.stat().st_ino == original.st_ino
    assert config.stat().st_mode == original.st_mode

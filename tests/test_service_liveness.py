"""Exercise runtime startup and the shipped shell watchdog without hardware."""

import builtins
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main

REPO = Path(__file__).resolve().parents[1]


def test_cold_start_primes_both_heartbeats_before_ticker_and_first_cycle(tmp_path, monkeypatch):
    runtime = tmp_path / "run/inverter-control"
    real_makedirs = os.makedirs

    def mkdir(path, **kwargs):
        assert path == "/run/inverter-control"
        real_makedirs(runtime, **kwargs)

    def open_heartbeat(path, *args, **kwargs):
        assert str(path).startswith("/run/inverter-control/")
        return builtins.open(runtime / Path(path).name, *args, **kwargs)

    def check_primed():
        assert int((runtime / "heartbeat").read_text()) > 0
        assert int((runtime / "inverter-control.heartbeat").read_text()) > 0
        return False

    ticker = Mock()
    ticker.start.side_effect = check_primed
    controller = Mock(loop_interval=0.33, grid_filter=None, derived_grid_filter=None)
    controller.run_cycle.side_effect = check_primed
    shutdown = Mock(return_value=True)
    monkeypatch.setattr(main, "os", SimpleNamespace(makedirs=mkdir))
    monkeypatch.setattr(main, "open", open_heartbeat, raising=False)
    monkeypatch.setattr(main.threading, "Thread", Mock(return_value=ticker))
    monkeypatch.setattr(main, "_shutdown_main_loop", shutdown)

    main._run_main_loop(controller, None)

    controller.run_cycle.assert_called_once()
    ticker.start.assert_called_once()
    shutdown.assert_called_once()


def test_runtime_directory_failure_does_not_start_workers(monkeypatch):
    controller = Mock()
    monkeypatch.setattr(main, "os", SimpleNamespace(makedirs=Mock(side_effect=PermissionError)))
    with pytest.raises(PermissionError):
        main._run_main_loop(controller, None)
    controller._watchdog.start.assert_not_called()
    controller.start_auxiliary_readers.assert_not_called()


@pytest.mark.parametrize("age", [-1, 0, 3, 4, 5, 9, 14, 15, 60, None])
def test_keepalive_freshness_spans_the_heartbeat_cadence(tmp_path, age):
    heartbeat = tmp_path / "heartbeat"
    if age is not None:
        heartbeat.write_text("1000")
        os.utime(heartbeat, (1000 - age, 1000 - age))
    script = tmp_path / "keepalive-check.sh"
    source = (REPO / "keepalive.sh").read_text().split("# ----------------", 1)[0]
    script.write_text(source + '\ndate() { echo 1000; }\nHEARTBEAT_FILE="$1"\nis_main_running\n')
    result = subprocess.run(
        ["sh", str(script), str(heartbeat)], capture_output=True, text=True, timeout=5
    )
    expected = age is not None and 0 <= age < 3 * main.HEARTBEAT_INTERVAL
    assert result.returncode == (0 if expected else 1), result.stderr


@pytest.fixture
def watchdog(tmp_path):
    """Run one real check per invocation with a fake clock and supervisor."""
    runtime = tmp_path / "run"
    service = tmp_path / "service/inverter-control"
    runtime.mkdir()
    service.mkdir(parents=True)
    source = (REPO / "service/watchdog/run").read_text().split('log "=== watchdog', 1)[0]
    source = source.replace("/run/inverter-control", str(runtime))
    source = source.replace("/service/", str(tmp_path / "service") + "/")
    script = tmp_path / "watchdog-check.sh"
    script.write_text(
        source
        + '\ndate() { echo "$TEST_NOW"; }\n'
        + 'svc() { echo "$1" >> "$TEST_COMMANDS"; return "$TEST_SVC_STATUS"; }\n'
        + 'check_service "inverter-control"\n'
    )
    commands = tmp_path / "commands"

    class Watchdog:
        heartbeat = runtime / "inverter-control.heartbeat"
        records = runtime / ".watchdog_restarts"
        backoff = runtime / ".watchdog_restart_backoff_inverter-control"
        legacy = runtime / ".watchdog_disabled_inverter-control"

        def check(self, now=1000, **options):
            env = {
                key: value for key, value in os.environ.items() if not key.startswith("WATCHDOG_")
            }
            result = subprocess.run(
                ["sh", str(script)],
                env={
                    **env,
                    "TEST_NOW": str(now),
                    "TEST_COMMANDS": str(commands),
                    "TEST_SVC_STATUS": "0",
                    **options,
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert result.returncode == 0, result.stderr
            assert not result.stderr
            return commands.read_text().splitlines() if commands.exists() else []

        def exhaust_restarts(self):
            self.heartbeat.write_text("1")
            for now in (1000, 1010, 1020):
                self.check(now)
            assert self.check(1030) == ["-k", "-u"] * 3 + ["-u"]
            assert self.backoff.exists()

    return Watchdog()


def test_watchdog_first_restart_has_no_empty_count_error(watchdog):
    watchdog.heartbeat.write_text("1")
    assert watchdog.check() == ["-k", "-u"]
    assert [record.read_text().strip() for record in watchdog.records.iterdir()] == ["1000"]


def test_restart_backoff_keeps_service_enabled_and_expires(watchdog):
    watchdog.exhaust_restarts()
    assert watchdog.check(1629) == ["-k", "-u"] * 3 + ["-u"]
    assert watchdog.check(1630) == ["-k", "-u"] * 3 + ["-u", "-k", "-u"]
    assert not watchdog.backoff.exists()
    assert len(list(watchdog.records.iterdir())) == 1


def test_healthy_tick_clears_backoff_and_prunes_records(watchdog):
    watchdog.exhaust_restarts()
    watchdog.heartbeat.write_text("1400")
    assert watchdog.check(1400) == ["-k", "-u"] * 3 + ["-u"]
    assert not watchdog.backoff.exists()
    assert not list(watchdog.records.iterdir())


def test_restart_history_prunes_expired_invalid_and_future_records(watchdog):
    watchdog.records.mkdir()
    for name, value in (("old", "699"), ("edge", "700"), ("bad", "no"), ("future", "1001")):
        (watchdog.records / f"inverter-control_{name}.time").write_text(value)
    watchdog.heartbeat.write_text("1000")
    assert watchdog.check() == []
    assert [p.name for p in watchdog.records.iterdir()] == ["inverter-control_edge.time"]


@pytest.mark.parametrize("svc_status", ["0", "1"])
def test_legacy_disable_is_released_only_after_successful_up(watchdog, svc_status):
    watchdog.legacy.write_text("999")
    assert watchdog.check(TEST_SVC_STATUS=svc_status) == ["-u"]
    assert watchdog.legacy.exists() == (svc_status != "0")


def test_alert_only_never_actuates_even_with_legacy_disable(watchdog):
    watchdog.legacy.write_text("999")
    watchdog.heartbeat.write_text("1")
    assert watchdog.check(WATCHDOG_ALERT_ONLY="1") == []
    assert watchdog.legacy.exists()
    assert not watchdog.records.exists()


def test_legacy_backoff_setting_is_still_honored(watchdog):
    watchdog.exhaust_restarts()
    assert watchdog.check(1640, WATCHDOG_DISABLE_BACKOFF="900") == ["-k", "-u"] * 3 + ["-u"]
    assert watchdog.check(1930, WATCHDOG_DISABLE_BACKOFF="900")[-2:] == ["-k", "-u"]


@pytest.mark.parametrize("content", [None, "", "not-a-timestamp"])
def test_missing_or_invalid_heartbeat_does_not_restart(watchdog, content):
    if content is not None:
        watchdog.heartbeat.write_text(content)
    assert watchdog.check() == []

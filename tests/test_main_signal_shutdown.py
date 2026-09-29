"""Exercise the shipped signal handler in isolated children, without hardware startup."""

import ast
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "main.py"


def _handler_source():
    text = SOURCE.read_text()
    node = next(
        node
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name == "signal_handler"
    )
    return ast.get_source_segment(text, node)


def _run_child(mode, signum, timeout):
    # The production handler, real ten-second Timer and real os._exit are not
    # shortened or mocked. Extracting only the handler avoids controller imports
    # or accidentally connecting to MQTT, D-Bus or Home Assistant in a child.
    child = (
        "import atexit,json,os,signal,sys,threading,time\n"
        f"mode={mode!r}\nsignum={int(signum)}\n"
        "started=time.monotonic()\n"
        "def record(event, **details):\n"
        " print(json.dumps(dict(event=event, elapsed=time.monotonic()-started, "
        "**details)), flush=True)\n"
        "class Logger:\n"
        " def warning(self, message):\n"
        "  timers=[t for t in threading.enumerate() if isinstance(t,threading.Timer)]\n"
        "  record('logging', watchdogs=[dict(daemon=t.daemon,interval=t.interval) "
        "for t in timers])\n"
        "  if mode=='blocked-log-repeat': threading.Event().wait()\n"
        "logger=Logger()\n" + _handler_source() + "\n"
        "atexit.register(lambda:record('atexit'))\n"
        "signal.signal(signum,signal_handler)\n"
        "if mode=='blocked-thread':\n"
        " threading.Thread(target=threading.Event().wait,daemon=False).start()\n"
        "if mode=='blocked-log-repeat':\n"
        " def repeat():\n"
        "  time.sleep(5)\n"
        "  os.kill(os.getpid(),signum)\n"
        " threading.Thread(target=repeat,daemon=True).start()\n"
        "try:\n"
        " os.kill(os.getpid(),signum)\n"
        "finally:\n"
        " record('cleanup-started')\n"
        " if mode=='blocked-cleanup': threading.Event().wait()\n"
        " record('cleanup-finished')\n"
    )
    started = time.monotonic()
    with subprocess.Popen(
        [sys.executable, "-B", "-c", child],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            pytest.fail(f"Shutdown exceeded {timeout}s: {stdout!r}; {stderr!r}")
        assert process.returncode == 0, stderr
        assert not stderr
    return [json.loads(line) for line in stdout.splitlines()], time.monotonic() - started


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_signal_exits_without_waiting_for_watchdog(signum):
    events, elapsed = _run_child("normal", signum, timeout=3)
    assert [event["event"] for event in events] == [
        "logging",
        "cleanup-started",
        "cleanup-finished",
        "atexit",
    ]
    assert events[0]["watchdogs"] == [{"daemon": True, "interval": 10.0}]
    assert elapsed < 3


@pytest.mark.parametrize("mode", ["blocked-cleanup", "blocked-thread", "blocked-log-repeat"])
def test_watchdog_still_forces_exit_when_shutdown_blocks(mode):
    events, elapsed = _run_child(mode, signal.SIGTERM, timeout=14)
    assert 9.5 <= elapsed < 14
    names = [event["event"] for event in events]
    assert "atexit" not in names, "The real os._exit bypasses normal exit hooks"
    logging = [event for event in events if event["event"] == "logging"]
    assert logging[0]["watchdogs"] == [{"daemon": True, "interval": 10.0}]
    if mode == "blocked-log-repeat":
        assert len(logging) == 2
        assert 4.5 <= logging[1]["elapsed"] < 9
        assert logging[1]["watchdogs"] == [
            {"daemon": True, "interval": 10.0},
            {"daemon": True, "interval": 10.0},
        ]
        assert "cleanup-started" not in names
        # The second signal at five seconds must not reset the first deadline.
        # A reset would keep the child alive for fifteen seconds and time out.
    else:
        assert "cleanup-started" in names
        assert ("cleanup-finished" in names) == (mode == "blocked-thread")

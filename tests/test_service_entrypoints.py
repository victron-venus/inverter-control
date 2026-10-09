"""Execute service entry points in a private fake filesystem, without a device."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "service,target",
    [("inverter-control", "main.py"), ("log-forwarder", "inverter_control/log_forwarder.py")],
)
def test_python_service_uses_existing_module_and_unbuffered_output(tmp_path, service, target):
    package = tmp_path / "package"
    package.mkdir()
    destination = package / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("# fixture module, never executed\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python3"
    python.write_text(
        '#!/bin/sh\n[ "$1" = "-u" ] || exit 30\n'
        '[ "$2" = "$EXPECTED_MODULE" ] || exit 31\n'
        '[ -f "$2" ] || exit 32\nprintf "%s\\n" "$PWD"\n'
    )
    python.chmod(0o755)
    script = (
        (ROOT / f"service/{service}/run")
        .read_text()
        .replace("/data/inverter-control", str(package))
    )
    run = tmp_path / "run"
    run.write_text(script)
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "EXPECTED_MODULE": target,
    }
    result = subprocess.run(["sh", str(run)], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == str(package)


@pytest.mark.parametrize("service", ["inverter-control", "log-forwarder", "watchdog"])
def test_service_loggers_create_directory_and_use_available_multilog(tmp_path, service):
    log_dir = tmp_path / "logs" / service
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    multilog = bin_dir / "multilog"
    multilog.write_text(
        '#!/bin/sh\n[ "$1" = t ] && [ "$2" = s25000 ] && [ "$3" = n4 ] || exit 30\n'
        '[ "$4" = "$EXPECTED_LOG" ] && [ -d "$4" ] || exit 31\n'
        'printf "logger started\\n"\n'
    )
    multilog.chmod(0o755)
    script = (
        (ROOT / f"service/{service}/log/run")
        .read_text()
        .replace(f"/var/log/{service}", str(log_dir))
    )
    run = tmp_path / "run"
    run.write_text(script)
    env = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "EXPECTED_LOG": str(log_dir),
    }
    result = subprocess.run(["sh", str(run)], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "logger started"


def test_noninteractive_setup_rejects_unselected_action_before_prompt(tmp_path):
    called = tmp_path / "prompt-called"
    helpers = tmp_path / "helpers"
    helpers.write_text(
        'scriptAction=NONE\nstandardActionPrompt() { echo called > "$PROMPT_MARKER"; }\n'
    )
    script = (
        (ROOT / "setup")
        .read_text()
        .replace("/data/SetupHelper/HelperResources/IncludeHelpers", str(helpers))
    )
    run = tmp_path / "setup"
    run.write_text(script)
    result = subprocess.run(
        ["bash", str(run)],
        stdin=subprocess.DEVNULL,
        env=os.environ | {"PROMPT_MARKER": str(called)},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert "refusing to prompt" in result.stderr
    assert not called.exists()

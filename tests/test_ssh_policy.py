"""Administrative SSH helper arguments; fake ssh never opens a connection."""

import json
import os
import shutil
import subprocess  # nosec B404 - fixed local shell plus fixture-only fake SSH
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_ssh(tmp_path):
    binary = tmp_path / "ssh"
    trace = tmp_path / "trace.jsonl"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['SSH_TEST_TRACE'], 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == '-G':\n"
        "    print(os.environ['SSH_TEST_CONFIG'])\n"
        "    sys.exit(int(os.environ.get('SSH_TEST_QUERY_STATUS', '0')))\n"
        "sys.exit(int(os.environ.get('SSH_TEST_STATUS', '0')))\n"
    )
    binary.chmod(0o700)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SSH_TEST_TRACE": str(trace)}
    return env, trace


def invoke(shell, fake_ssh, config, **extra):
    env, trace = fake_ssh
    executable = shutil.which(shell)
    assert executable is not None
    result = subprocess.run(  # nosec B603 - argv uses fixed code and fixture data, no network
        [
            executable,
            "-c",
            '. "$1"; shift; ssh_with_key_policy "$@"',
            "test",
            str(ROOT / "inverter_control/ssh_policy.sh"),
            "host alias",
            "printf 'remote command'",
        ],
        env={**env, "SSH_TEST_CONFIG": config, **extra},
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result, [json.loads(line) for line in trace.read_text().splitlines()]


@pytest.mark.parametrize("shell", ["sh", "bash"])
@pytest.mark.parametrize("value,expected", [("1024", "2048"), ("2048", "2048"), ("4096", "4096")])
def test_effective_minimum_preserves_stricter_settings_and_arguments(
    shell, value, expected, fake_ssh
):
    result, calls = invoke(
        shell, fake_ssh, f"hostname alias.example\nrequiredrsasize {value}\nproxyjump gateway"
    )
    assert result.returncode == 0
    assert calls == [
        ["-G", "host alias", "printf 'remote command'"],
        [
            "-S",
            "none",
            "-o",
            f"RequiredRSASize={expected}",
            "host alias",
            "printf 'remote command'",
        ],
    ]


@pytest.mark.parametrize(
    "config",
    [
        "",
        "hostname example",
        "requiredrsasize",
        "requiredrsasize 2x",
        "requiredrsasize -1",
        "requiredrsasize 2048 extra",
        "requiredrsasize 2048\nrequiredrsasize 4096",
        "requiredrsasize " + "9" * 1000,
    ],
)
def test_invalid_effective_configuration_never_starts_network_ssh(config, fake_ssh):
    result, calls = invoke("sh", fake_ssh, config)
    assert result.returncode == 2
    assert calls == [["-G", "host alias", "printf 'remote command'"]]


def test_query_failure_and_remote_status_are_preserved(fake_ssh):
    result, calls = invoke("sh", fake_ssh, "requiredrsasize 4096", SSH_TEST_QUERY_STATUS="42")
    assert result.returncode == 42
    assert calls == [["-G", "host alias", "printf 'remote command'"]]


def test_network_failure_is_not_reported_as_success(fake_ssh):
    result, calls = invoke("sh", fake_ssh, "requiredrsasize 4096", SSH_TEST_STATUS="43")
    assert result.returncode == 43
    assert len(calls) == 2


def test_restart_entrypoint_uses_policy(fake_ssh):
    env, trace = fake_ssh
    executable = shutil.which("sh")
    assert executable is not None
    result = subprocess.run(  # nosec B603 - actual local entrypoint with fixture-only fake SSH
        [executable, str(ROOT / "restart.sh")],
        env={**env, "SSH_TEST_CONFIG": "requiredrsasize 4096"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert calls == [
        ["-G", "Cerbo", "svc -t /service/inverter-control"],
        ["-S", "none", "-o", "RequiredRSASize=4096", "Cerbo", "svc -t /service/inverter-control"],
    ]

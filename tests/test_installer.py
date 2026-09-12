"""Run the native updater in an isolated fake Venus filesystem."""

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def fake_device(tmp_path: Path, *, python_status: int = 0, fresh_heartbeat: bool = True):
    data = tmp_path / "data"
    services = tmp_path / "service"
    package = data / "inverter-control"
    package.mkdir(parents=True)
    services.mkdir()
    (data / "rc.local").write_text("#!/bin/sh\n# another package\nexit 0\n")
    (package / "local_config.py").write_text("USER_SETTING = 42\n")
    (package / "main.py").write_text("# existing controller\n")
    (package / "keepalive.sh").write_text("#!/bin/sh\nexit 0\n")
    (package / "local_config.example.py").write_text("USER_SETTING = 0\n")
    (package / "inverter_control").mkdir()
    (package / "inverter_control/__init__.py").write_text("")
    (package / "version").write_text("v1.0\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    heartbeat_dir = tmp_path / "run/inverter-control"
    heartbeat_dir.mkdir(parents=True)
    if fresh_heartbeat:
        import time

        (heartbeat_dir / "inverter-control.heartbeat").write_text(str(int(time.time()) + 10))
    for command in ("sleep", "svc", "python3", "svstat"):
        path = bin_dir / command
        status = python_status if command == "python3" else 0
        path.write_text(f'#!/bin/sh\necho {command} >> "{tmp_path}/commands"\nexit {status}\n')
        if command == "svstat":
            path.write_text(
                '#!/bin/sh\necho "/service/inverter-control: up (pid 1234) 3 seconds"\n'
            )
        path.chmod(0o755)
    for name in ("inverter-control", "log-forwarder", "watchdog"):
        service = package / "service" / name
        (service / "log").mkdir(parents=True)
        (service / "run").write_text("#!/bin/sh\nexit 0\n")
        (service / "log/run").write_text("#!/bin/sh\nexit 0\n")
        (service / "supervise").mkdir()
        (service / "supervise/status").write_text("active supervisor")
        (services / name).symlink_to(service)
    script = (REPO / "update.sh").read_text().replace("/data/", f"{data}/")
    script = re.sub(r"(?<![A-Za-z0-9_./}])/service", str(services), script)
    script = script.replace("/var/log/", f"{tmp_path}/log/")
    script = script.replace("/run/inverter-control", str(heartbeat_dir))
    (package / "update.sh").write_text(script)
    return package, services, {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_in_place_updates_preserve_source_config_and_supervisors(tmp_path):
    package, services, env = fake_device(tmp_path)
    inode = (package / "service/inverter-control/supervise").stat().st_ino
    for _ in range(2):
        subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env, check=True)
        assert (package / "main.py").read_text() == "# existing controller\n"
        assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"
        assert (package / "service/inverter-control/supervise").stat().st_ino == inode
        assert (services / "inverter-control").resolve() == package / "service/inverter-control"
    hook = (package.parent / "rc.local").read_text()
    assert hook.count("# === inverter-control service persistence ===") == 1
    assert hook.index("# === end inverter-control ===") < hook.index("exit 0")


def test_dependency_failure_does_not_stop_running_services(tmp_path):
    package, _, env = fake_device(tmp_path, python_status=23)
    result = subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env)
    assert result.returncode == 23
    assert "svc" not in (tmp_path / "commands").read_text()
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"


def test_staged_update_refreshes_code_without_replacing_supervisors(tmp_path):
    import shutil

    package, _, env = fake_device(tmp_path)
    stage = tmp_path / "release"
    shutil.copytree(package, stage)
    (stage / "main.py").write_text("# new controller\n")
    inode = (package / "service/inverter-control/supervise").stat().st_ino
    subprocess.run(["sh", str(stage / "update.sh"), str(package)], cwd=stage, env=env, check=True)
    assert (package / "main.py").read_text() == "# new controller\n"
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"
    assert (package / "service/inverter-control/supervise").stat().st_ino == inode


def test_failed_startup_is_not_reported_as_installed(tmp_path):
    package, _, env = fake_device(tmp_path, fresh_heartbeat=False)
    result = subprocess.run(
        ["sh", "update.sh", str(package)], cwd=package, env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "fresh heartbeat" in result.stderr
    assert "installed version" not in result.stdout

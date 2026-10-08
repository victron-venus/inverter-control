"""Run the native updater in an isolated fake Venus filesystem."""

import os
import re

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
from pathlib import Path

import pytest

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
    (package / "setup_ssl.sh").write_text((REPO / "setup_ssl.sh").read_text())
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
        # Test harness intentionally uses its fixture-controlled PATH.
        subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env, check=True)  # nosec B603, B607
        assert (package / "main.py").read_text() == "# existing controller\n"
        assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"
        assert (package / "local_config.py").stat().st_mode & 0o777 == 0o600
        assert (package / "service/inverter-control/supervise").stat().st_ino == inode
        assert (services / "inverter-control").resolve() == package / "service/inverter-control"
    hook = (package.parent / "rc.local").read_text()
    assert hook.count("# === inverter-control service persistence ===") == 1
    assert hook.index("# === end inverter-control ===") < hook.index("exit 0")


def test_dependency_failure_does_not_stop_running_services(tmp_path):
    package, _, env = fake_device(tmp_path, python_status=23)
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env)  # nosec B603, B607
    assert result.returncode == 23
    assert "svc" not in (tmp_path / "commands").read_text()
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"


def test_staged_update_refreshes_code_without_replacing_supervisors(tmp_path):
    import shutil

    package, _, env = fake_device(tmp_path)
    stage = tmp_path / "release"
    shutil.copytree(package, stage)
    (stage / "main.py").write_text("# new controller\n")
    (package / "setup_ssl.sh").write_text(
        "# obsolete certificate and remote configuration helper\n"
    )
    (package / "metrics.env").write_text("INVERTER_METRICS_HOST=192.0.2.10\n")
    inode = (package / "service/inverter-control/supervise").stat().st_ino
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["sh", str(stage / "update.sh"), str(package)], cwd=stage, env=env, check=True)  # nosec B603, B607
    assert (package / "main.py").read_text() == "# new controller\n"
    assert (package / "setup_ssl.sh").read_text() == (REPO / "setup_ssl.sh").read_text()
    assert (package / "metrics.env").read_text() == "INVERTER_METRICS_HOST=192.0.2.10\n"
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"
    assert (package / "service/inverter-control/supervise").stat().st_ino == inode


def test_failed_startup_is_not_reported_as_installed(tmp_path):
    package, _, env = fake_device(tmp_path, fresh_heartbeat=False)
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(  # nosec B603, B607
        ["sh", "update.sh", str(package)], cwd=package, env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "fresh heartbeat" in result.stderr
    assert "installed version" not in result.stdout


def tariff_python_stub(tmp_path, package):
    """Keep fake device dependency checks isolated but run the real tariff validator."""
    import shlex
    import sys

    (package / "inverter_control/tariff.py").write_text(
        (REPO / "inverter_control/tariff.py").read_text().replace("/data/", f"{tmp_path}/data/")
    )
    stub = tmp_path / "bin/python3"
    stub.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        f'  */tariff.py) exec {shlex.quote(sys.executable)} "$@" ;;\n'
        "  *) exit 0 ;;\nesac\n"
    )


def test_invalid_deploy_tariff_rejected_before_service_stop(tmp_path):
    package, _, env = fake_device(tmp_path)
    tariff_python_stub(tmp_path, package)
    (package / "tariff-install.json").write_text("{}")
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(  # nosec B603, B607
        ["sh", "update.sh", str(package)], cwd=package, env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert not (tmp_path / "commands").exists()
    assert (package / "main.py").read_text() == "# existing controller\n"


def test_explicit_deploy_tariff_persists_and_ordinary_update_preserves_it(tmp_path):
    import json

    from test_tariff import schedule

    package, _, env = fake_device(tmp_path)
    tariff_python_stub(tmp_path, package)
    incoming = package / "tariff-install.json"
    incoming.write_text(json.dumps(schedule()))
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env, check=True)  # nosec B603, B607
    saved = package.parent / "setupOptions/inverter-control/electricity-tariff.json"
    content = saved.read_text()
    assert json.loads(content)["billingDay"] == 17
    incoming.unlink()
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env, check=True)  # nosec B603, B607
    assert saved.read_text() == content


def test_setup_uninstall_removes_both_hook_variants_and_preserves_other_content(tmp_path):
    package, _, env = fake_device(tmp_path)
    data = package.parent
    rc = data / "rc.local"
    rc.chmod(0o750)
    rc.write_text(
        "#!/bin/sh\n# another package\n"
        "# === inverter-control persistence ===\nold hook\n"
        "# === end inverter-control ===\n"
        "# === inverter-control service persistence ===\nnew hook\n"
        "# === end inverter-control ===\nexit 0\n"
    )
    helpers = data / "SetupHelper/HelperResources/IncludeHelpers"
    helpers.parent.mkdir(parents=True)
    helpers.write_text(
        "scriptAction=UNINSTALL\npackageName=inverter-control\n"
        "logMessage() { :; }\nremoveDbusSettings() { :; }\nendScript() { :; }\n"
    )
    script = (REPO / "setup").read_text().replace("/data/", f"{data}/")
    script = script.replace("/service/", f"{tmp_path}/service/")
    (package / "setup").write_text(script)
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(  # nosec B603, B607
        ["bash", "setup"], cwd=package, env=env, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert rc.read_text() == "#!/bin/sh\n# another package\nexit 0\n"
    assert rc.stat().st_mode & 0o777 == 0o750
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"
    assert not list(data.glob(".inverter-control-uninstall.*"))


@pytest.mark.parametrize("legacy", [False, True])
def test_bootstrap_and_legacy_config_are_private(tmp_path, legacy):
    package, _, env = fake_device(tmp_path)
    config = package / "local_config.py"
    config.unlink()
    if legacy:
        old_config = package / "secrets.py"
        old_config.write_text("LEGACY_SETTING = 73\n")
        old_config.chmod(0o644)
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env, check=True)  # nosec B603, B607
    expected = "LEGACY_SETTING = 73\n" if legacy else "USER_SETTING = 0\n"
    assert config.read_text() == expected
    assert config.stat().st_mode & 0o777 == 0o600
    assert not (package / "secrets.py").exists()


@pytest.mark.parametrize("staged", [False, True])
def test_push_config_restricts_source_live_and_persistent_copies(tmp_path, staged):
    import shutil

    package, _, env = fake_device(tmp_path)
    stage = tmp_path / "release" if staged else package
    if staged:
        shutil.copytree(package, stage)
    source = stage / "local_config.py"
    source.write_text("PUSHED_SETTING = 17\n")
    source.chmod(0o644)
    persistent = package.parent / "setupOptions/inverter-control/local_config.py"
    persistent.parent.mkdir(parents=True)
    persistent.write_text("OLD_SETTING = 0\n")
    persistent.chmod(0o644)
    env["PUSH_LOCAL_CONFIG"] = "1"
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["sh", str(stage / "update.sh"), str(package)], env=env, check=True)  # nosec B603, B607
    for config in (source, package / "local_config.py", persistent):
        assert config.read_text() == "PUSHED_SETTING = 17\n"
        assert config.stat().st_mode & 0o777 == 0o600


def test_preserved_persistent_config_also_gets_private_permissions(tmp_path):
    package, _, env = fake_device(tmp_path)
    persistent = package.parent / "setupOptions/inverter-control/local_config.py"
    persistent.parent.mkdir(parents=True)
    persistent.write_text("PERSISTENT_SETTING = 18\n")
    persistent.chmod(0o644)
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env, check=True)  # nosec B603, B607
    assert persistent.read_text() == "PERSISTENT_SETTING = 18\n"
    assert persistent.stat().st_mode & 0o777 == 0o600


def test_config_permission_failure_aborts_before_stopping_services(tmp_path):
    package, _, env = fake_device(tmp_path)
    chmod = tmp_path / "bin/chmod"
    chmod.write_text("#!/bin/sh\nexit 43\n")
    chmod.chmod(0o755)
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(["sh", "update.sh", str(package)], cwd=package, env=env)  # nosec B603, B607
    assert result.returncode == 43
    assert "svc" not in (tmp_path / "commands").read_text()
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"


def test_new_config_permission_failure_keeps_services_down_and_reports_failure(tmp_path):
    import shlex
    import shutil

    package, _, env = fake_device(tmp_path)
    config = package / "local_config.py"
    config.unlink()
    real_chmod = shutil.which("chmod")
    assert real_chmod is not None
    chmod = tmp_path / "bin/chmod"
    chmod.write_text(
        f'#!/bin/sh\ncase "$1" in 600) exit 43 ;; esac\nexec {shlex.quote(real_chmod)} "$@"\n'
    )
    chmod.chmod(0o755)
    (tmp_path / "bin/svc").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{tmp_path}/service-actions"\n'
    )
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(  # nosec B603, B607
        ["sh", "update.sh", str(package)], cwd=package, env=env, capture_output=True, text=True
    )
    assert result.returncode == 43
    assert "installed version" not in result.stdout
    assert config.read_text() == "USER_SETTING = 0\n"
    # The creation mask protects the copy even when the final chmod fails.
    assert config.stat().st_mode & 0o777 == 0o600
    assert not any(
        action.startswith("-u ")
        for action in (tmp_path / "service-actions").read_text().splitlines()
    )


@pytest.mark.parametrize("kind", ["live", "persistent", "legacy"])
def test_config_symlink_rejected_without_changing_its_target(tmp_path, kind):
    package, _, env = fake_device(tmp_path)
    target = tmp_path / "unrelated.py"
    target.write_text("UNRELATED = 19\n")
    target.chmod(0o644)
    if kind == "persistent":
        config = package.parent / "setupOptions/inverter-control/local_config.py"
        config.parent.mkdir(parents=True)
    elif kind == "legacy":
        (package / "local_config.py").unlink()
        config = package / "secrets.py"
    else:
        config = package / "local_config.py"
        config.unlink()
    config.symlink_to(target)
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(  # nosec B603, B607
        ["sh", "update.sh", str(package)], cwd=package, env=env, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "regular file" in result.stderr
    assert "svc" not in (tmp_path / "commands").read_text()
    assert target.read_text() == "UNRELATED = 19\n"
    assert target.stat().st_mode & 0o777 == 0o644


def test_missing_crypto_dependency_precedes_service_stop(tmp_path):
    import sys

    package, _, env = fake_device(tmp_path)
    python = tmp_path / "bin/python3"
    python.write_text(f"""#!{sys.executable}
import builtins,sys
original=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.startswith("cryptography"):
        raise ImportError("synthetic missing cryptography")
    return original(name,*args,**kwargs)
builtins.__import__=guarded
exec(compile(sys.stdin.read(),"installer-preflight","exec"))
""")
    # Execute the real installer using only the isolated fake-device command PATH.
    result = subprocess.run(  # nosec B603, B607
        ["sh", "update.sh", str(package)],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "synthetic missing cryptography" in result.stderr
    assert not (tmp_path / "commands").exists()
    assert (package / "local_config.py").read_text() == "USER_SETTING = 42\n"

"""PackageManager settings survive headless and automatic installation."""

import json
import os
import pty

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
from pathlib import Path

import pytest
from runtime_fixtures import real_selector_bootstrap
from test_installer import fake_device

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("option", [None, "true\n", "false\n"])
def test_automatic_setup_does_not_prompt_and_keeps_private_config(tmp_path, option):
    data, package, script = prepare_setup(tmp_path, option)
    master, slave = pty.openpty()
    try:
        # Test harness intentionally uses its fixture-controlled PATH.
        result = subprocess.run(  # nosec B603, B607
            ["bash", str(script)],
            stdin=slave,
            capture_output=True,
            text=True,
            timeout=5,
        )
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 0, result.stderr
    assert "Enter true/false" not in result.stdout
    assert "Minimum [" not in result.stdout
    assert '{"max": 2250, "min": -2300}' in result.stdout
    assert (package / "local_config.py").read_text() == "SITE_PRIVATE = 42\n"
    assert (package / "local_config.py").stat().st_mode & 0o777 == 0o600
    private_source = data / "setupOptions/inverter-control/local_config.py"
    assert private_source.stat().st_mode & 0o777 == 0o600
    assert (data / "updated").exists()
    option_path = data / "setupOptions/inverter-control/use_grid_submeter_as_backup"
    assert option_path.read_text() == option if option is not None else not option_path.exists()


def test_invalid_option_stops_before_replacing_config_or_starting_update(tmp_path):
    data, package, script = prepare_setup(tmp_path, "enable")
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=5)  # nosec B603, B607
    assert result.returncode == 1 and "must contain true or false" in result.stderr
    assert not (data / "updated").exists()
    assert (package / "local_config.py").read_text() == "EXISTING_PRIVATE = 9\n"


def prepare_setup(tmp_path, option):
    package, _, _ = fake_device(tmp_path)
    data = package.parent
    helpers = data / "SetupHelper/HelperResources"
    helpers.mkdir(parents=True)
    options = data / "setupOptions/inverter-control"
    options.mkdir(parents=True)
    (options / "local_config.py").write_text("SITE_PRIVATE = 42\n")
    (package / "local_config.py").write_text("EXISTING_PRIVATE = 9\n")
    if option is not None:
        (options / "use_grid_submeter_as_backup").write_text(option)
    (helpers / "IncludeHelpers").write_text(
        "scriptAction=INSTALL\nuserInteraction=false\npackageName=inverter-control\n"
        f'scriptDir="{package}"\n'
        "logMessage() { :; }\nendScript() { :; }\n"
    )
    (package / "update.sh").rename(package / "updater-test.sh")
    (package / "update.sh").write_text(
        f'#!/bin/sh\nPATH="{tmp_path}/bin:$PATH" sh "{package}/updater-test.sh" "$@" || exit $?\n'
        f'touch "{data}/updated"\n'
    )
    script = tmp_path / "setup"
    script.write_text(
        real_selector_bootstrap((REPO / "setup").read_text().replace("/data/", f"{data}/"))
    )
    return data, package, script


@pytest.mark.parametrize("cleared", [False, True])
def test_automatic_setup_validates_tariff_without_prompting(tmp_path, cleared):
    import json

    from test_tariff import schedule

    data, package, script = prepare_setup(tmp_path, None)
    (package / "inverter_control").mkdir(exist_ok=True)
    (package / "inverter_control/tariff.py").write_text(
        (REPO / "inverter_control/tariff.py").read_text()
    )
    path = data / "setupOptions/inverter-control/electricity-tariff.json"
    content = json.dumps(None if cleared else schedule())
    path.write_text(content)
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(  # nosec B603, B607
        ["bash", str(script)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert "Configure electricity" not in result.stdout
    assert path.read_text() == content
    assert (data / "updated").exists()


def test_invalid_setup_tariff_stops_before_replacing_private_config(tmp_path):
    data, package, script = prepare_setup(tmp_path, None)
    (package / "inverter_control").mkdir(exist_ok=True)
    (package / "inverter_control/tariff.py").write_text(
        (REPO / "inverter_control/tariff.py").read_text()
    )
    path = data / "setupOptions/inverter-control/electricity-tariff.json"
    path.write_text("{}")
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=5)  # nosec B603, B607
    assert result.returncode == 1
    assert not (data / "updated").exists()
    assert (package / "local_config.py").read_text() == "EXISTING_PRIVATE = 9\n"
    assert path.read_text() == "{}"


def test_setup_copy_creates_private_file_when_live_config_is_absent(tmp_path):
    data, package, script = prepare_setup(tmp_path, None)
    config = package / "local_config.py"
    config.unlink()
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=5)  # nosec B603, B607
    assert result.returncode == 0, result.stderr
    assert config.read_text() == "SITE_PRIVATE = 42\n"
    assert config.stat().st_mode & 0o777 == 0o600
    assert (data / "updated").exists()


def test_setup_permission_failure_does_not_copy_or_start_update(tmp_path):
    data, package, script = prepare_setup(tmp_path, None)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    chmod = bin_dir / "chmod"
    chmod.write_text("#!/bin/sh\nexit 43\n")
    chmod.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)  # nosec B603, B607
    assert result.returncode == 43
    assert (package / "local_config.py").read_text() == "EXISTING_PRIVATE = 9\n"
    assert not (data / "updated").exists()


@pytest.mark.parametrize("kind", ["source", "destination"])
def test_setup_rejects_private_config_symlinks(tmp_path, kind):
    data, package, script = prepare_setup(tmp_path, None)
    target = tmp_path / "unrelated.py"
    target.write_text("UNRELATED = 19\n")
    target.chmod(0o644)
    config = (
        data / "setupOptions/inverter-control/local_config.py"
        if kind == "source"
        else package / "local_config.py"
    )
    config.unlink()
    config.symlink_to(target)
    # Test harness intentionally uses its fixture-controlled PATH.
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=5)  # nosec B603, B607
    assert result.returncode == 1
    assert "regular file" in result.stderr
    assert target.read_text() == "UNRELATED = 19\n"
    assert target.stat().st_mode & 0o777 == 0o644
    assert not (data / "updated").exists()


def test_setup_validates_persistent_config_limits_before_replacing_live_config(tmp_path):
    data, package, script = prepare_setup(tmp_path, None)
    persistent = data / "setupOptions/inverter-control/local_config.py"
    persistent.write_text("POWER_LIMIT_MIN = -3100\nPOWER_LIMIT_MAX = 2000\n")
    result = subprocess.run(  # nosec B603, B607 -- fake device updater
        ["bash", str(script)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    assert "Power limits:" in result.stderr
    assert (package / "local_config.py").read_text() == "EXISTING_PRIVATE = 9\n"
    assert "svc" not in (tmp_path / "commands").read_text()
    assert not (data / "updated").exists()


def test_setup_selects_persistent_limits_instead_of_installed_limits(tmp_path):
    data, package, script = prepare_setup(tmp_path, None)
    persistent = data / "setupOptions/inverter-control/local_config.py"
    content = "POWER_LIMIT_MIN = -1700\nPOWER_LIMIT_MAX = 1900\n"
    persistent.write_text(content)
    (package / "local_config.py").write_text("POWER_LIMIT_MIN = -500\nPOWER_LIMIT_MAX = 500\n")
    result = subprocess.run(  # nosec B603, B607 -- fake device updater
        ["bash", str(script)], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert '{"max": 1900, "min": -1700}' in result.stdout
    assert (package / "local_config.py").read_text() == content
    assert not (persistent.parent / "power-limits.json").exists()


def test_setup_rejects_conflicting_private_config_authorities(tmp_path):
    data, package, script = prepare_setup(tmp_path, None)
    result = subprocess.run(  # nosec B603, B607 -- fake device updater
        ["bash", str(script)],
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PUSH_LOCAL_CONFIG": "1"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "cannot both be enabled" in result.stderr
    assert (package / "local_config.py").read_text() == "EXISTING_PRIVATE = 9\n"
    assert not (data / "updated").exists()


@pytest.mark.parametrize(
    "answers,expected", [("\n\n", None), ("-1750\n1900\n", {"min": -1750, "max": 1900})]
)
def test_interactive_setup_keeps_defaults_or_saves_explicit_limits(tmp_path, answers, expected):
    data, _, script = prepare_setup(tmp_path, None)
    helpers = data / "SetupHelper/HelperResources/IncludeHelpers"
    helpers.write_text(helpers.read_text().replace("userInteraction=false", "userInteraction=true"))
    master, slave = pty.openpty()
    try:
        # Empty backup and tariff answers, followed by the two power-limit answers.
        os.write(master, ("\n\n" + answers).encode())
        result = subprocess.run(  # nosec B603, B607 -- fake device updater and PTY
            ["bash", str(script)], stdin=slave, capture_output=True, text=True, timeout=8
        )
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 0, result.stderr
    assert "Minimum [-2300 W]" in result.stdout
    assert "Maximum [2250 W]" in result.stdout
    path = data / "setupOptions/inverter-control/power-limits.json"
    if expected is None:
        assert not path.exists()
    else:
        assert json.loads(path.read_text()) == expected

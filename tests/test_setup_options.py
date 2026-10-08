"""PackageManager settings survive headless and automatic installation."""

import os
import pty

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
from pathlib import Path

import pytest

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
    data = tmp_path / "data"
    helpers = data / "SetupHelper/HelperResources"
    helpers.mkdir(parents=True)
    package = data / "inverter-control"
    package.mkdir()
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
    (package / "update.sh").write_text(f'#!/bin/sh\ntouch "{data}/updated"\n')
    script = tmp_path / "setup"
    script.write_text((REPO / "setup").read_text().replace("/data/", f"{data}/"))
    return data, package, script


@pytest.mark.parametrize("cleared", [False, True])
def test_automatic_setup_validates_tariff_without_prompting(tmp_path, cleared):
    import json

    from test_tariff import schedule

    data, package, script = prepare_setup(tmp_path, None)
    (package / "inverter_control").mkdir()
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
    (package / "inverter_control").mkdir()
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
    bin_dir.mkdir()
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

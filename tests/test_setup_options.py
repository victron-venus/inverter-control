"""PackageManager settings survive headless and automatic installation."""

import os
import pty
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("option", [None, "true\n", "false\n"])
def test_automatic_setup_does_not_prompt_and_keeps_private_config(tmp_path, option):
    data, package, script = prepare_setup(tmp_path, option)
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
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
    assert (data / "updated").exists()
    option_path = data / "setupOptions/inverter-control/use_grid_submeter_as_backup"
    assert option_path.read_text() == option if option is not None else not option_path.exists()


def test_invalid_option_stops_before_replacing_config_or_starting_update(tmp_path):
    data, package, script = prepare_setup(tmp_path, "enable")
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=5)
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

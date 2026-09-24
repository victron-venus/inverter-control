"""Exercise the deployment archive using a fake SSH endpoint, never a real Cerbo."""

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from test_tariff import schedule

REPO = Path(__file__).resolve().parents[1]


def test_deploy_tariff_opt_in_and_archive_path(tmp_path):
    root, bin_dir = tmp_path / "repo", tmp_path / "bin"
    root.mkdir()
    bin_dir.mkdir()
    shutil.copy(REPO / "deploy.sh", root)
    shutil.copytree(REPO / "inverter_control", root / "inverter_control")
    (root / "main.py").write_text("")
    (root / "electricity-tariff.json").write_text(
        "private local file is not an implicit deployment"
    )
    archive = tmp_path / "received.tar.gz"
    (bin_dir / "ssh").write_text(
        f'#!/bin/sh\ncase "$2" in\n  svstat*) exit 0 ;;\n  *) cat > "{archive}" ;;\nesac\n'
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for file in bin_dir.iterdir():
        file.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "PUSH_LOCAL_CONFIG": "0"}
    env.pop("TARIFF_FILE", None)
    subprocess.run(
        ["bash", "deploy.sh", "fake-device"], cwd=root, env=env, check=True, capture_output=True
    )
    with tarfile.open(archive) as package:
        assert not any(
            "tariff-install.json" in name or "electricity-tariff.json" in name
            for name in package.getnames()
        )
    tariff = tmp_path / "input with spaces.json"
    tariff.write_text(json.dumps(schedule()))
    env["TARIFF_FILE"] = str(tariff)
    subprocess.run(
        ["bash", "deploy.sh", "fake-device"], cwd=root, env=env, check=True, capture_output=True
    )
    with tarfile.open(archive) as package:
        # update.sh extracts with --strip-components=1; the ./ prefix is required.
        content = json.load(package.extractfile("./tariff-install.json"))
        assert content["version"] == 2 and content["billingDay"] == 17
    archive.unlink()
    tariff.write_text("{}")
    result = subprocess.run(
        ["bash", "deploy.sh", "fake-device"], cwd=root, env=env, capture_output=True
    )
    assert result.returncode != 0 and not archive.exists()

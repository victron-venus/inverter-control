"""Exercise the actual release workflow archive command in a clean staging tree."""

import os
import re
import shutil
import subprocess
import tarfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_release_archive_contains_complete_installer_payload(tmp_path):
    workflow = (REPO / ".github/workflows/publish-release.yml").read_text()
    archive_step = workflow.split("    - name: Create PackageManager archive\n", 1)[1]
    command = archive_step.split("      run: |\n", 1)[1].split("\n    - name:", 1)[0]
    command = textwrap.dedent(command).replace("${{ steps.version.outputs.VERSION }}", "v1.0.0")
    for name in (
        "main.py",
        "inverter_control",
        "version",
        "gitHubInfo",
        "setup",
        "update.sh",
        "keepalive.sh",
        "local_config.example.py",
        "service",
    ):
        source = REPO / name
        target = tmp_path / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target)
    subprocess.run(
        ["sh", "-eu", "-c", command],
        cwd=tmp_path,
        check=True,
        env={**os.environ, "GITHUB_OUTPUT": str(tmp_path / "output")},
    )
    with tarfile.open(tmp_path / "inverter-control-v1.0.0.tar.gz") as archive:
        members = set(archive.getnames())
        runtime_items = re.search(
            r'^RUNTIME_ITEMS="([^"]+)"', (REPO / "update.sh").read_text(), re.MULTILINE
        )
        assert runtime_items is not None
        for name in runtime_items.group(1).split():
            assert "inverter-control/" + name in members
        assert "inverter-control/local_config.py" not in members
        example = archive.extractfile("inverter-control/local_config.example.py")
        assert example is not None
        compile(example.read(), "local_config.example.py", "exec")
        for name in ("inverter-control", "log-forwarder", "watchdog"):
            assert f"inverter-control/service/{name}/run" in members
            assert f"inverter-control/service/{name}/log/run" in members

"""Exercise the candidate packaging command in a clean tracked staging tree."""

import json
import re
import shutil

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import package_release

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("channel", ["--root=/outside", "stable", "unknown"])
def test_invalid_channel_fails_before_adapter_or_output(tmp_path, channel):
    """Direct callers cannot pass unchecked channel arguments to the adapter."""
    (tmp_path / ".release-policy.json").write_text(
        json.dumps({"mode": "release", "versioning": {}})
    )
    (tmp_path / ".release-package.json").write_text("{}")
    output = tmp_path / "artifacts"
    with patch.object(package_release.subprocess, "run") as command:
        with pytest.raises(ValueError, match="Stable releases must promote"):
            package_release.build_candidate(tmp_path, "1.2.3", channel, output)
    command.assert_not_called()
    assert not output.exists()


def test_release_archive_contains_complete_installer_payload(tmp_path):
    config = json.loads((REPO / ".release-package.json").read_text())
    inputs = config["include"] + [
        ".release-policy.json",
        ".release-package.json",
        "scripts/package-release.sh",
        "scripts/package_release.py",
        "scripts/release_version_adapter.py",
        "scripts/release_control.py",
        "scripts/version_plan.py",
    ]
    for name in inputs:
        source = REPO / name
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif source.is_file():
            shutil.copy2(source, target)
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # nosec B603, B607
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(["git", "add", "--", "."], cwd=tmp_path, check=True)  # nosec B603, B607
    (tmp_path / "local_config.py").write_text("OPERATOR_SETTING = 'local fixture'\n")
    version = (tmp_path / "version").read_text().strip().removeprefix("v")
    # Test harness intentionally uses its fixture-controlled PATH.
    subprocess.run(  # nosec B603, B607
        ["bash", "scripts/package-release.sh", version, "rc"],
        cwd=tmp_path,
        check=True,
    )
    # Touch source timestamps and build again: neither local mtimes nor gzip time
    # may change the source payload or checksum manifest.
    import os

    os.utime(tmp_path / "main.py", (1234567890, 1234567890))
    subprocess.run(  # nosec B603, B607
        ["bash", "scripts/package-release.sh", version, "rc", "--output", str(tmp_path / "repeat")],
        cwd=tmp_path,
        check=True,
    )
    for asset in (tmp_path / "release-dist").iterdir():
        assert asset.read_bytes() == (tmp_path / "repeat" / asset.name).read_bytes()
    with tarfile.open(tmp_path / "release-dist" / f"inverter-control-{version}.tar.gz") as archive:
        members = set(archive.getnames())
        runtime_items = re.search(
            r'^RUNTIME_ITEMS="([^"]+)"', (REPO / "update.sh").read_text(), re.MULTILINE
        )
        assert runtime_items is not None
        for name in runtime_items.group(1).split():
            source = tmp_path / name
            payload = (
                [path for path in source.rglob("*") if path.is_file()]
                if source.is_dir()
                else [source]
            )
            assert payload
            for path in payload:
                assert "inverter-control/" + path.relative_to(tmp_path).as_posix() in members
        assert "inverter-control/local_config.py" not in members
        example = archive.extractfile("inverter-control/local_config.example.py")
        assert example is not None
        compile(example.read(), "local_config.example.py", "exec")
        for name in ("inverter-control", "log-forwarder", "watchdog"):
            assert f"inverter-control/service/{name}/run" in members
            assert f"inverter-control/service/{name}/log/run" in members

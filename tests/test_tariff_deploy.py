"""Exercise the deployment archive using a fake SSH endpoint, never a real Cerbo."""

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from test_tariff import schedule

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment(tmp_path):
    root, bin_dir = tmp_path / "repo", tmp_path / "bin"
    root.mkdir()
    bin_dir.mkdir()
    shutil.copy(REPO / "deploy.sh", root)
    shutil.copytree(REPO / "inverter_control", root / "inverter_control")
    (root / "main.py").write_text("")
    (root / "electricity-tariff.json").write_text(
        "private runtime fallback is not an implicit deployment"
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
    return root, archive, env


def run_deploy(deployment):
    root, _, env = deployment
    return subprocess.run(
        ["bash", str(root / "deploy.sh"), "fake-device"],
        cwd=root.parent,  # Defaults belong to the checkout, not the caller's cwd.
        env=env,
        capture_output=True,
        text=True,
    )


def local_tariff(root, content):
    directory = root / "deploy.local"
    directory.mkdir()
    (directory / "tariff.json").write_text(content)
    (directory / "provenance.json").write_text('{"private":"local source notes"}')
    return directory / "tariff.json"


def packaged_tariff(archive):
    with tarfile.open(archive) as package:
        names = package.getnames()
        assert not any(
            "deploy.local" in name or "electricity-tariff.json" in name for name in names
        )
        # update.sh extracts with --strip-components=1; the ./ prefix is required.
        if "./tariff-install.json" in names:
            return json.load(package.extractfile("./tariff-install.json"))
        return None


def test_deploy_without_selection_preserves_device_tariff(deployment):
    _, archive, _ = deployment
    result = run_deploy(deployment)
    assert result.returncode == 0, result.stderr
    assert packaged_tariff(archive) is None


def test_deploy_saved_default_is_normalized_and_private_notes_are_excluded(deployment):
    root, archive, _ = deployment
    local_tariff(root, json.dumps(schedule()))
    result = run_deploy(deployment)
    assert result.returncode == 0, result.stderr
    content = packaged_tariff(archive)
    assert content["version"] == 2 and content["billingDay"] == 17


def test_deploy_explicit_file_overrides_invalid_saved_default(deployment, tmp_path):
    root, archive, env = deployment
    local_tariff(root, "invalid default")
    tariff = tmp_path / "input with spaces.json"
    selected = {**schedule(), "name": "Explicit override"}
    tariff.write_text(json.dumps(selected))
    env["TARIFF_FILE"] = str(tariff)
    result = run_deploy(deployment)
    assert result.returncode == 0, result.stderr
    assert packaged_tariff(archive)["name"] == "Explicit override"


def test_deploy_explicit_empty_preserves_device_even_with_invalid_default(deployment):
    root, archive, env = deployment
    local_tariff(root, "invalid default")
    env["TARIFF_FILE"] = ""
    result = run_deploy(deployment)
    assert result.returncode == 0, result.stderr
    assert packaged_tariff(archive) is None


@pytest.mark.parametrize(
    "selection",
    ["invalid_default", "broken_default_link", "invalid_override", "missing_override"],
)
def test_bad_selected_tariff_fails_before_ssh_without_fallback(deployment, selection):
    root, archive, env = deployment
    default = local_tariff(root, json.dumps(schedule()))
    if selection == "invalid_default":
        default.write_text("{}")
    elif selection == "broken_default_link":
        default.unlink()
        default.symlink_to(root / "missing.json")
    else:
        explicit = root / "explicit.json"
        env["TARIFF_FILE"] = str(explicit)
        if selection == "invalid_override":
            explicit.write_text("{}")
    result = run_deploy(deployment)
    assert result.returncode != 0
    assert not archive.exists()

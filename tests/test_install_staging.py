"""Exercise real installation entry points without Venus OS or live services."""

import importlib.util
import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("staged_install", REPO / "install.py")
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.mark.parametrize(
    "entrypoint", [["make", "install"], ["bash", "setup", "install", "auto"], ["sh", "update.sh"]]
)
def test_actual_entrypoints_stage_without_live_dependencies(tmp_path, entrypoint):
    root = tmp_path / "staging root"
    env = {**os.environ, "DESTDIR": str(root)}
    # These commands contain only test-controlled arguments and run the real adapter.
    subprocess.run(entrypoint, cwd=REPO, env=env, check=True)  # nosec B603
    package = root / "data/inverter-control"
    assert (package / "main.py").read_bytes() == (REPO / "main.py").read_bytes()
    assert (package / "service/inverter-control/run").stat().st_mode & 0o111
    assert not (package / "local_config.py").exists()
    assert not (root / "service").exists()
    assert not (root / "data/rc.local").exists()
    assert not list(package.rglob("*.pyc"))
    (package / "local_config.py").write_text("operator_owned = True\n")
    subprocess.run(["make", "uninstall"], cwd=REPO, env=env, check=True)  # nosec B603, B607
    assert not (package / "main.py").exists()
    assert (package / "local_config.py").read_text() == "operator_owned = True\n"


@pytest.mark.parametrize("destdir", ["", "/"])
def test_live_filesystem_is_not_a_staging_root(destdir):
    with pytest.raises(ValueError, match="DESTDIR"):
        installer.stage(REPO, destdir)


def test_symlink_destination_rejected_before_any_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "stage").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer.stage(REPO, str(tmp_path / "stage"))
    assert list(outside.iterdir()) == []


def test_symlink_payload_target_is_not_followed(tmp_path):
    target = tmp_path / "stage/data/inverter-control"
    target.mkdir(parents=True)
    secret = tmp_path / "outside"
    secret.write_text("untouched")
    (target / "main.py").symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        installer.stage(REPO, str(tmp_path / "stage"))
    assert secret.read_text() == "untouched"
    assert not (target / "version").exists()


@pytest.mark.parametrize(
    "name", ["../../outside", "/outside", "local_config.py", "version/../local_config.py"]
)
def test_manifest_cannot_remove_operator_files(tmp_path, name):
    installer.stage(REPO, str(tmp_path))
    package = tmp_path / "data/inverter-control"
    (package / installer.MANIFEST).write_text(json.dumps([name]))
    with pytest.raises(ValueError, match="manifest"):
        installer.unstage(str(tmp_path))
    assert (package / "main.py").exists()


def test_staging_cli_needs_explicit_safe_root(tmp_path):
    env = {**os.environ, "DESTDIR": ""}
    result = subprocess.run(
        [sys.executable, str(REPO / "install.py"), "install"], env=env, capture_output=True
    )  # nosec B603
    assert result.returncode != 0

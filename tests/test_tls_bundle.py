"""Fail closed when assembling the offline ARMv7 dependency artifact."""

import hashlib
import json
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.finalize_tls_bundle import EXPECTED, finalize


def wheel(directory, name, version, suffix=""):
    path = directory / f"{name}-{version}{suffix}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n"
        )
    return path


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_REVISION", "a" * 40)
    (tmp_path / "wheels").mkdir()
    for name, version in EXPECTED.items():
        wheel(tmp_path / "wheels", name, version)
    return tmp_path


def test_complete_bundle_records_actual_hashes_and_revision(bundle):
    finalize(bundle)
    manifest = json.loads((bundle / "provenance.json").read_text())
    assert manifest["source_revision"] == "a" * 40
    assert len(manifest["wheels"]) == 3
    requirements = []
    for entry in manifest["wheels"]:
        actual = hashlib.sha256((bundle / "wheels" / entry["file"]).read_bytes()).hexdigest()
        assert entry["sha256"] == actual
        requirements.append(f"{entry['name']}=={entry['version']} --hash=sha256:{actual}\n")
    assert (bundle / "requirements.txt").read_text() == "".join(requirements)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "wrong_version", "metadata"])
def test_incomplete_or_unexpected_wheel_is_rejected_without_manifest(bundle, change):
    directory = bundle / "wheels"
    if change == "missing":
        next(directory.glob("cffi-*.whl")).unlink()
    elif change == "extra":
        wheel(directory, "unrequested", "1.0")
    elif change == "duplicate":
        wheel(directory, "cffi", EXPECTED["cffi"], "-copy")
    elif change == "wrong_version":
        next(directory.glob("cffi-*.whl")).unlink()
        wheel(directory, "cffi", "1.0")
    else:
        with zipfile.ZipFile(next(directory.glob("cffi-*.whl")), "a") as archive:
            archive.writestr("extra.dist-info/METADATA", "Name: extra\nVersion: 1\n")
    with pytest.raises(ValueError):
        finalize(bundle)
    assert not (bundle / "provenance.json").exists()
    assert not (bundle / "requirements.txt").exists()


@pytest.mark.parametrize("revision", ["", "main", "a" * 39, "x" * 40])
def test_source_revision_must_be_explicit_commit(bundle, monkeypatch, revision):
    monkeypatch.setenv("SOURCE_REVISION", revision)
    with pytest.raises(ValueError, match="SOURCE_REVISION"):
        finalize(bundle)


def test_bundle_versions_match_runtime_lock():
    root = Path(__file__).resolve().parents[1]
    lock = tomllib.loads((root / "uv.lock").read_text())
    versions = {item["name"]: item["version"] for item in lock["package"]}
    assert {name: versions[name] for name in EXPECTED} == EXPECTED

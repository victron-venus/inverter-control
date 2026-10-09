"""Reject incomplete, redirected and unauthenticated release payloads."""

import hashlib
import os
import subprocess  # nosec B404
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts import release_signatures as signatures
from scripts.release_signing_overlay import overlay

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def assets(tmp_path):
    names = ["inverter-control-1.23.5.tar.gz", "tls-dependencies-cp312-armv7-1.23.5.tar.gz"]
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(name.encode()).hexdigest()}  {name}\n" for name in names)
    )
    (tmp_path / signatures.BUNDLE).write_text("signature fixture")
    return tmp_path


def test_complete_native_and_source_payloads_are_checked(assets):
    assert len(signatures.payload_inventory(assets)) == 2
    next(assets.glob("inverter-control-*.tar.gz")).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        signatures.payload_inventory(assets)


@pytest.mark.parametrize("name", ["../outside", "/outside", "-option", "other.tar.gz"])
def test_unsigned_or_unsafe_inventory_name_is_rejected(assets, name):
    (assets / "SHA256SUMS").write_text(f"{'a' * 64}  {name}\n")
    with pytest.raises(ValueError):
        signatures.payload_inventory(assets)


def test_unsigned_extra_payload_is_rejected(assets):
    (assets / "extra.whl").write_bytes(b"extra")
    with pytest.raises(ValueError, match="inventory"):
        signatures.payload_inventory(assets)


def test_linked_payload_is_rejected(assets):
    target = next(assets.glob("inverter-control-*.tar.gz"))
    content = target.read_bytes()
    target.unlink()
    (assets.parent / "outside").write_bytes(content)
    target.symlink_to(assets.parent / "outside")
    with pytest.raises(ValueError, match="regular"):
        signatures.payload_inventory(assets)


def test_authentication_failure_stops_before_payload_processing(assets):
    with (
        patch.object(
            signatures.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "cosign")
        ),
        patch.object(signatures, "payload_inventory") as inventory,
    ):
        with pytest.raises(subprocess.CalledProcessError):
            signatures.verify_signature(assets, "a" * 40)
    inventory.assert_not_called()


def test_verification_binds_expected_identity_issuer_and_reviewed_source(assets):
    with patch.object(signatures.subprocess, "run") as command:
        signatures.verify_signature(assets, "b" * 40)
    argv = command.call_args.args[0]
    assert argv[argv.index("--certificate-identity") + 1] == signatures.IDENTITY
    assert argv[argv.index("--certificate-oidc-issuer") + 1] == signatures.ISSUER
    assert argv[argv.index("--certificate-github-workflow-sha") + 1] == "b" * 40
    assert command.call_args.kwargs == {"check": True}


def test_signing_overlay_restores_regenerated_permission_idempotently(tmp_path):
    original = (REPO / ".github/workflows/release-pipeline.yml").read_text()
    path = tmp_path / "release-pipeline.yml"
    # Simulate the generator's output without the local permission overlay.
    path.write_text(original.replace("      id-token: write\n", ""))
    with pytest.raises(ValueError, match="Missing signing permission"):
        overlay(path, check=True)
    overlay(path)
    assert path.read_text() == original
    overlay(path)
    assert path.read_text() == original


def test_release_workflow_scopes_identity_token_to_signer_and_requires_verification():
    # BaseLoader constructs only strings, lists and mappings, never Python objects.
    workflow = yaml.load(  # nosec B506
        (REPO / ".github/workflows/release-build.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert "id-token" not in workflow["permissions"]
    assert "id-token" not in workflow["jobs"]["package"].get("permissions", {})
    signer = workflow["jobs"]["sign"]
    assert signer["permissions"]["id-token"] == "write"
    assert signer["runs-on"] == "ubuntu-latest"
    assert signer["needs"] == "package"
    commands = [step.get("run", "") for step in signer["steps"]]
    assert any("verify-build signed-release-assets" in cmd for cmd in commands)
    verify = next(
        index for index, cmd in enumerate(commands) if "verify signed-release-assets" in cmd
    )
    upload = next(
        index
        for index, step in enumerate(signer["steps"])
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert verify < upload
    overlay(REPO / ".github/workflows/release-pipeline.yml", check=True)


@pytest.mark.parametrize("kind", ["extra", "fifo"])
def test_build_verification_rejects_untrusted_entries_before_reading(assets, kind):
    extra = assets / "unexpected"
    if kind == "fifo":
        os.mkfifo(extra)
    else:
        extra.write_text("unexpected")
    with patch.object(Path, "read_bytes", side_effect=AssertionError("unsafe read")):
        with pytest.raises(ValueError):
            signatures.verify_build(assets, assets / "absent-plan", assets / "absent-policy")

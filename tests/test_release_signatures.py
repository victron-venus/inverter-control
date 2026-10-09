"""Reject incomplete, redirected and unauthenticated release payloads."""

import hashlib
import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts import release_signatures as signatures
from scripts.release_signing_overlay import overlay

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def assets(tmp_path):
    tmp_path = tmp_path / "assets"
    tmp_path.mkdir()
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


@pytest.mark.parametrize("separator", [" ", "   ", "\t", " \t"])
def test_inventory_requires_exactly_two_spaces(assets, separator):
    sums = assets / "SHA256SUMS"
    sums.write_text(sums.read_text().replace("  ", separator))
    with pytest.raises(ValueError, match="Invalid or duplicate"):
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
    assert (
        argv[argv.index("--certificate-github-workflow-repository") + 1]
        == "victron-venus/inverter-control"
    )
    assert argv[argv.index("--certificate-github-workflow-ref") + 1] == "refs/heads/main"
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
    assert "github.repository == 'victron-venus/inverter-control'" in signer["if"]
    assert "github.ref == 'refs/heads/main'" in signer["if"]
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


@pytest.fixture
def build_documents(assets, monkeypatch):
    """Real plan and receipt validators, with control files outside the assets."""
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    import version_plan

    root = assets.parent
    (root / "version").write_text("1.23.5\n")
    policy = {
        "repository": signatures.REPOSITORY,
        "mode": "release",
        "version_file": "version",
        "versioning": {
            "schema": 1,
            "promotion": "final-build",
            "files": [{"path": "version", "format": "text", "value": "full"}],
        },
    }
    plan = version_plan.create_plan("1.23.5", "rc", 1, "a" * 40, policy)
    files = version_plan.sync_versions(root, policy, plan)
    receipt = {
        "source_sha": plan["source_sha"],
        "plan_sha256": version_plan.plan_digest(plan),
        "effective_inputs_sha256": version_plan.effective_inputs_digest(files),
        "files": files,
        "toolchain": {"python": "3.12.13"},
        "artifacts": [
            {
                "name": p.name,
                "size": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in assets.iterdir()
        ],
    }
    (assets / "release-inputs-package.json").write_text(json.dumps(receipt))
    plan_path, policy_path = root / ".release-plan.json", root / ".release-policy.json"
    plan_path.write_text(json.dumps(plan))
    policy_path.write_text(json.dumps(policy))
    return plan_path, policy_path


@pytest.mark.parametrize("mode", ["default", "absolute", "relative-parent", "symlink"])
def test_build_cli_accepts_explicit_control_files_outside_checkout(
    assets, build_documents, mode, tmp_path
):
    plan, policy = build_documents
    command = [
        sys.executable,
        str(REPO / "scripts/release_signatures.py"),
        "verify-build",
        str(assets),
    ]
    cwd = tmp_path
    if mode == "relative-parent":
        cwd = tmp_path / "working"
        cwd.mkdir()
        plan, policy = Path("..") / plan.name, Path("..") / policy.name
    elif mode == "symlink":
        for path in (plan, policy):
            path.with_suffix(".alias").symlink_to(path)
        plan, policy = plan.with_suffix(".alias"), policy.with_suffix(".alias")
    if mode != "default":
        command.extend(["--plan", str(plan), "--policy", str(policy)])
    # Run only this repository's CLI against private test files, without a shell.
    result = subprocess.run(  # nosec B603
        command, cwd=cwd, capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field", ["--plan", "--policy"])
def test_build_cli_rejects_fifo_without_waiting_for_writer(assets, build_documents, field):
    plan, policy = build_documents
    fifo = assets.parent / "control-fifo"
    os.mkfifo(fifo)
    command = [
        sys.executable,
        str(REPO / "scripts/release_signatures.py"),
        "verify-build",
        str(assets),
        "--plan",
        str(plan),
        "--policy",
        str(policy),
    ]
    command[command.index(field) + 1] = str(fifo)
    # A timeout also bounds this regression if the nonblocking guard is removed.
    result = subprocess.run(  # nosec B603
        command, capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    assert "must be a regular file" in result.stderr


def test_control_document_size_limit_counts_bytes_and_keeps_exact_boundary(tmp_path):
    path = tmp_path / "control.json"
    path.write_bytes(b"{}" + b" " * (signatures.MAX_CONTROL_BYTES - 2))
    assert signatures.read_control_document(path) == {}
    path.write_bytes(b"{}" + b" " * (signatures.MAX_CONTROL_BYTES - 1))
    with pytest.raises(ValueError, match="1 MiB limit"):
        signatures.read_control_document(path)
    # Below the character limit but above the byte limit in UTF-8.
    path.write_text(
        json.dumps({"value": "é" * (signatures.MAX_CONTROL_BYTES // 2)}, ensure_ascii=False)
    )
    with pytest.raises(ValueError, match="1 MiB limit"):
        signatures.read_control_document(path)


@pytest.mark.parametrize("content", ["null", "[]", "true", "42", '"text"'])
def test_control_document_requires_json_object(tmp_path, content):
    path = tmp_path / "control.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="JSON object"):
        signatures.read_control_document(path)


def test_control_document_rejects_directory(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        signatures.read_control_document(tmp_path)

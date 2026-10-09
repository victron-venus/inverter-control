"""Validate and authenticate the complete downloadable runtime payload inventory."""

import argparse
import hashlib
import json
import re
import subprocess  # nosec B404
from pathlib import Path

ISSUER = "https://token.actions.githubusercontent.com"
REPOSITORY = "victron-venus/inverter-control"
REF = "refs/heads/main"
IDENTITY = "https://github.com/victron-venus/inverter-control/.github/workflows/release-build.yml@refs/heads/main"
CHECKSUMS = "SHA256SUMS"
BUNDLE = "SHA256SUMS.sigstore.json"
METADATA = {
    CHECKSUMS,
    BUNDLE,
    "release-inputs-package.json",
    "release-inputs-signature.json",
    "release-manifest.json",
}


def payload_inventory(directory: Path) -> dict[str, str]:
    """Allow only the two documented payload types, with matching base versions."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Release assets must be a regular directory")
    entries = list(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ValueError("Release assets must be flat regular files")
    text = (directory / CHECKSUMS).read_text(encoding="ascii")
    inventory = {}
    versions = set()
    kinds = set()
    for line in text.splitlines():
        match = re.fullmatch(
            r"([a-f0-9]{64})  ((inverter-control|tls-dependencies-cp312-armv7)-(\d+\.\d+\.\d+)\.tar\.gz)",
            line,
            re.ASCII,
        )
        if match is None or match[2] in inventory:
            raise ValueError("Invalid or duplicate release checksum entry")
        inventory[match[2]] = match[1]
        kinds.add(match[3])
        versions.add(match[4])
    if (
        kinds != {"inverter-control", "tls-dependencies-cp312-armv7"}
        or len(inventory) != 2
        or len(versions) != 1
    ):
        raise ValueError("Checksums must cover source and ARMv7 payloads of one version")
    if {path.name for path in entries} - METADATA != set(inventory):
        raise ValueError("Release payload inventory differs from signed checksums")
    for name, expected in inventory.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Release payload checksum mismatch: {name}")
    return inventory


def verify_signature(directory: Path, source_sha: str) -> None:
    if re.fullmatch(r"[a-f0-9]{40}", source_sha) is None:
        raise ValueError("Expected reviewed source commit SHA")
    # Authenticate the checksums before parsing or following their contents.
    for name in (CHECKSUMS, BUNDLE):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Signature inputs must be regular files")
    # The operator supplies Cosign through PATH; identities are fixed constants.
    subprocess.run(
        [  # nosec B603, B607
            "cosign",
            "verify-blob",
            "--bundle",
            str(directory / BUNDLE),
            "--certificate-identity",
            IDENTITY,
            "--certificate-oidc-issuer",
            ISSUER,
            "--certificate-github-workflow-sha",
            source_sha,
            "--certificate-github-workflow-repository",
            REPOSITORY,
            "--certificate-github-workflow-ref",
            REF,
            str(directory / CHECKSUMS),
        ],
        check=True,
    )
    payload_inventory(directory)


def verify_build(directory: Path, plan_path: Path, policy_path: Path) -> None:
    payload_inventory(directory)
    # These siblings are the reviewed, vendored publication validators.
    from version_plan import validate_plan
    from version_receipt import verify_receipts

    plan = validate_plan(json.loads(plan_path.read_text()))
    payloads = [
        {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in directory.iterdir()
    ]
    verify_receipts(directory, plan, payloads, json.loads(policy_path.read_text()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["verify", "verify-build", "inventory"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--plan", type=Path, default=Path(".release-plan.json"))
    parser.add_argument("--policy", type=Path, default=Path(".release-policy.json"))
    args = parser.parse_args()
    if args.action == "verify":
        verify_signature(args.directory, args.source_sha or "")
    elif args.action == "verify-build":
        verify_build(args.directory, args.plan, args.policy)
    else:
        payload_inventory(args.directory)


if __name__ == "__main__":
    main()

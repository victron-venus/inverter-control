"""Build candidate artifacts from a tracked snapshot, never from local secrets."""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


def read_version(root: Path, policy: dict[str, Any]) -> str:
    """Read the same committed metadata used by release selection."""
    path = root / policy["version_file"]
    if path.suffix == ".toml":
        value = tomllib.loads(path.read_text())["project"]["version"]
    elif path.suffix == ".json":
        value = json.loads(path.read_text())["version"]
    else:
        value = path.read_text().strip()
    return str(value).removeprefix("v")


def snapshot(root: Path, destination: Path) -> list[str]:
    """Copy tracked, present regular files; ignore untracked operator configuration."""
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    selected = []
    for name in sorted(set(filter(None, names))):
        source = root / name
        # These local agent settings and generated reports are not distribution inputs.
        if name == ".mcp.json" or name.startswith((".releases/", "analysis/")):
            continue
        if source.is_symlink():
            raise ValueError(f"Symlink is not a permitted release input: {name}")
        if not source.exists():
            continue
        if not source.is_file():
            raise ValueError(f"Release input must be a regular file: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        selected.append(name)
    return selected


def archive(snapshot_root: Path, names: list[str], output: Path, project: str) -> None:
    """Write a deterministic native/source archive preserving executable bits."""
    with output.open("wb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as package:
                for name in names:
                    source = snapshot_root / name
                    content = source.read_bytes()
                    entry = tarfile.TarInfo(f"{project}/{name}")
                    entry.size = len(content)
                    entry.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
                    package.addfile(entry, io.BytesIO(content))


def build_candidate(
    root: Path, version: str, channel: str, output: Path, *, containers: bool = True
) -> list[Path]:
    """Build all assets from one tracked snapshot and record their hashes."""
    policy = json.loads((root / ".release-policy.json").read_text())
    config = json.loads((root / ".release-package.json").read_text())
    if policy.get("mode") != "release":
        raise ValueError("This repository only supports validation, not product releases")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Expected the numeric base release version X.Y.Z")
    if read_version(root, policy) != version:
        raise ValueError("Candidate version must match project metadata")
    if channel not in {"nightly", "beta", "rc"}:
        raise ValueError("Stable releases must promote an existing RC without rebuilding")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Release output must be empty; stale artifacts are not accepted")
    project = policy["repository"].split("/")[1]
    with TemporaryDirectory(prefix="candidate-source-") as directory:
        source = Path(directory) / project
        source.mkdir()
        names = snapshot(root, source)
        for required in config.get("required", []):
            if not any(name == required or name.startswith(required + "/") for name in names):
                raise ValueError(f"Required input is missing or not tracked: {required}")
        includes = config.get("include")
        archive_names = [
            name
            for name in names
            if not includes or any(name == item or name.startswith(item + "/") for item in includes)
        ]
        archive(source, archive_names, output / f"{project}-{version}.tar.gz", project)
        if config.get("python_distribution"):
            # Build backend writes remain in this snapshot, including egg-info.
            subprocess.run(
                [
                    "uv",
                    "build",
                    "--sdist",
                    "--wheel",
                    "--no-create-gitignore",
                    "--build-constraints",
                    str(root / "scripts/build-constraints.txt"),
                    "--out-dir",
                    str(output),
                    str(source),
                ],
                check=True,
            )
            distributions = sorted(output.glob("*.whl")) + sorted(
                output.glob("solar_forecast_langgraph-*.tar.gz")
            )
            if len(distributions) != 2:
                raise ValueError("Expected one wheel and one Python source distribution")
            subprocess.run(
                [
                    "uvx",
                    "--from",
                    "twine==6.1.0",
                    "twine",
                    "check",
                    "--strict",
                    *map(str, distributions),
                ],
                check=True,
            )
        if containers and config.get("containers"):
            host = subprocess.check_output(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                text=True,
            ).strip()
            host = os.environ.get("DOCKER_HOST", host)
            if not host.startswith(("unix://", "npipe://")):
                raise ValueError("Release builds require a local Docker endpoint")
            for asset, image in config["containers"].items():
                if Path(asset).name != asset or not asset.endswith(".oci.tar"):
                    raise ValueError("OCI asset names must be simple .oci.tar filenames")
                if asset not in policy.get("container_assets", {}):
                    raise ValueError(f"OCI asset has no promotion target: {asset}")
                context = (source / image["context"]).resolve()
                dockerfile = (source / image["dockerfile"]).resolve()
                context.relative_to(source)
                dockerfile.relative_to(source)
                subprocess.run(
                    [
                        "docker",
                        "buildx",
                        "build",
                        "--platform",
                        image.get("platforms", "linux/amd64"),
                        "--provenance=true",
                        "--sbom=true",
                        "--file",
                        str(dockerfile),
                        "--output",
                        f"type=oci,dest={output / asset}",
                        str(context),
                    ],
                    check=True,
                )
    assets = sorted(path for path in output.iterdir() if path.is_file())
    lines = []
    for asset in assets:
        with asset.open("rb") as content:
            digest = hashlib.file_digest(content, "sha256").hexdigest()
        lines.append(f"{digest}  {asset.name}\n")
    (output / "SHA256SUMS").write_text("".join(lines))
    return assets


def main() -> None:
    """Expose the exact same package build to CI and local operators."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("channel", choices=["nightly", "beta", "rc"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for artifact in build_candidate(
        root, args.version, args.channel, args.output or root / "release-dist"
    ):
        sys.stdout.write(f"{artifact}\n")


if __name__ == "__main__":
    main()

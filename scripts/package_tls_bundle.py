"""Add the verified ARMv7 dependency closure to the native release payloads."""

import argparse
import hashlib
import json
from pathlib import Path

from package_release import archive


def package(bundle: Path, output: Path, version: str) -> None:
    source_archive = output / f"inverter-control-{version}.tar.gz"
    if not source_archive.is_file():
        raise ValueError("Build the native source archive first")
    evidence = json.loads((bundle / "verification.json").read_text())
    repeatability = json.loads((bundle / "repeatability.json").read_text())
    if not evidence or repeatability.get("clean_builds") != 2:
        raise ValueError("TLS bundle requires native verification and clean-build evidence")
    names = []
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink():
            raise ValueError("TLS bundle must not contain symlinks")
        if path.is_file():
            names.append(path.relative_to(bundle).as_posix())
    archive(bundle, names, output / f"tls-dependencies-cp312-armv7-{version}.tar.gz", "tls-armv7")
    assets = sorted(output.glob("*.tar.gz"))
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--bundle", type=Path, default=Path("release-dist/tls-armv7"))
    parser.add_argument("--output", type=Path, default=Path("release-dist"))
    args = parser.parse_args()
    package(args.bundle, args.output, args.version)

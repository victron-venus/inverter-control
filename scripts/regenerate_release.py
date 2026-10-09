"""Regenerate shared release files, then reapply and verify local signing policy."""

import argparse
import subprocess  # nosec B404
import sys
from pathlib import Path

from release_signing_overlay import overlay


def regenerate(toolkit: Path, root: Path) -> None:
    generator = toolkit.resolve(strict=True) / "scripts/install_release.py"
    # An explicitly chosen, reviewed local toolkit checkout; no shell evaluation.
    subprocess.run([sys.executable, str(generator), str(root)], check=True)  # nosec B603
    overlay(root / ".github/workflows/release-pipeline.yml")
    overlay(root / ".github/workflows/release-pipeline.yml", check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("toolkit", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    regenerate(args.toolkit, args.root.resolve(strict=True))

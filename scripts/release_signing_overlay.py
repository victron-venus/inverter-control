"""Apply or check the minimal local permission overlay after toolkit generation."""

import argparse
from pathlib import Path

import yaml

START = "  build:\n"
END = "  gate:\n"


def overlay(path: Path, *, check: bool = False) -> None:
    source = path.read_text()
    if source.count(START) != 1 or source.count(END) != 1:
        raise ValueError("Generated release workflow structure changed; review signing overlay")
    before, rest = source.split(START)
    build, after = rest.split(END)
    parsed = yaml.load(START + build, Loader=yaml.BaseLoader)["build"]  # nosec B506
    if parsed.get("uses") != "./.github/workflows/release-build.yml":
        raise ValueError("Release builder changed; review signing permissions")
    permissions = parsed.get("permissions", {})
    expected = {"contents": "read", "actions": "read"}
    if permissions == {**expected, "id-token": "write"}:
        return
    if permissions != expected:
        raise ValueError("Unexpected release-builder permissions")
    if check:
        raise ValueError(
            "Missing signing permission; run release_signing_overlay.py after generation"
        )
    marker = "      actions: read\n"
    if build.count(marker) != 1:
        raise ValueError("Ambiguous release-builder permission block")
    build = build.replace(marker, marker + "      id-token: write\n")
    path.write_text(before + START + build + END + after)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    overlay(args.root / ".github/workflows/release-pipeline.yml", check=args.check)

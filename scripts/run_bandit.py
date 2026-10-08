"""One complete, fail-closed Bandit gate for local, pull-request and scheduled CI."""

import json
import os

# Fixed interpreter/module argv; shell is never enabled.
import subprocess  # nosec B404
import sys
from importlib.metadata import version
from pathlib import Path

# Direct script execution and module execution use the same package imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.bandit_sarif import convert  # noqa: E402


def bandit_command(report_path: Path) -> list[str]:
    """Return the scanner argv shared with real CLI discovery regressions."""
    return [
        sys.executable,
        "-m",
        "bandit",
        # Ignore repository-discovered INI files; YAML is the reviewed policy.
        # Bandit warns about this empty INI; stderr stays visible intentionally.
        "--ini",
        os.devnull,
        "-c",
        ".github/bandit.yml",
        # Override CLI defaults: .git is a file in worktrees, not a directory.
        "--exclude",
        "",
        "-r",
        ".",
        "-f",
        "json",
        "-o",
        str(report_path),
    ]


def verify_discovery(root: Path) -> None:
    """Check real CLI discovery in the same pinned scanner environment."""
    # Fixed unittest argv; no shell or external input.
    subprocess.run(  # nosec B603
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_bandit_discovery.py",
        ],
        cwd=root,
        check=True,
        timeout=60,
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    report_path = root / "bandit-results.json"
    sarif_path = root / "bandit-results.sarif"
    report_path.unlink(missing_ok=True)
    sarif_path.unlink(missing_ok=True)
    verify_discovery(root)
    # Fixed Bandit command under the current interpreter.
    result = subprocess.run(  # nosec B603
        bandit_command(report_path),
        cwd=root,
        check=False,
        timeout=300,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Bandit failed with exit status {result.returncode}")
    report = json.loads(report_path.read_text())
    # Conversion validates errors, metrics and every finding before publication.
    sarif = convert(report, version("bandit"))
    if result.returncode != int(bool(report["results"])):
        raise RuntimeError("Bandit exit status contradicts its reported findings")
    sarif_path.write_text(json.dumps(sarif, indent=2) + "\n")
    for finding in report["results"]:
        print(
            f"{finding['test_id']} {finding['filename']}:{finding['line_number']}: "
            f"{finding['issue_text']}"
        )
    if report["results"] or result.returncode:
        print("Every unresolved Bandit finding blocks CI.")
        return 1
    print(f"Bandit: {report['metrics']['_totals']['loc']} lines, zero findings/errors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

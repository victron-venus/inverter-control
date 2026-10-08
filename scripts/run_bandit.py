"""One complete, fail-closed Bandit gate for local, pull-request and scheduled CI."""

import json

# Fixed interpreter/module argv; shell is never enabled.
import subprocess  # nosec B404
import sys
from importlib.metadata import version
from pathlib import Path

from bandit_sarif import convert


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    report_path = root / "bandit-results.json"
    sarif_path = root / "bandit-results.sarif"
    report_path.unlink(missing_ok=True)
    sarif_path.unlink(missing_ok=True)
    # Fixed Bandit command under the current interpreter.
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-m",
            "bandit",
            "-c",
            ".github/bandit.yml",
            "-r",
            ".",
            "-f",
            "json",
            "-o",
            str(report_path),
        ],
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

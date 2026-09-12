#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Bandit can return zero after parser/plugin exceptions; validate its report too.
run_bandit() (
  report=$(mktemp)
  trap 'rm -f "$report"' EXIT
  if uvx --python 3.12 --from bandit==1.8.6 bandit -r . -lll \
      -x .git,.venv,.venv-ci,tests,scripts/release.py,scripts/release_control.py \
      --format json --output "$report"; then
    scanner_status=0
  else
    scanner_status=$?
  fi
  python3 - "$report" "$scanner_status" <<'PYCODE'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
errors = report.get("errors")
results = report.get("results")
if not isinstance(errors, list) or not isinstance(results, list):
    raise SystemExit("Bandit did not produce a complete structured report")
if errors:
    for error in errors:
        print("Bandit could not scan: " + str(error.get("filename", "unknown file")))
    raise SystemExit("Bandit scanner/parser errors are blocking")
lines = report.get("metrics", {}).get("_totals", {}).get("loc", 0)
if not isinstance(lines, (int, float)) or lines <= 0:
    raise SystemExit("Bandit scanned no source; refusing an empty security gate")
for result in results:
    print("{severity} {rule} {file}:{line}".format(
        severity=result["issue_severity"], rule=result["test_id"],
        file=result["filename"], line=result["line_number"],
    ))
status = int(sys.argv[2])
if status:
    raise SystemExit(status)
if any(result["issue_severity"] == "HIGH" for result in results):
    raise SystemExit("Bandit HIGH findings are blocking")
print("Bandit scanned {} source lines without scanner errors or HIGH findings".format(lines))
PYCODE
)
if [[ "${1:-}" == security || "${1:-}" == bandit ]]; then
  run_bandit
  if [[ "${1:-}" == security ]]; then
    command -v trivy >/dev/null || { echo 'Trivy is required for the complete local security gate.' >&2; exit 1; }
    trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --exit-code 1 --skip-dirs .git,.venv,.venv-ci,release-dist,dist,build .
  fi
  exit 0
fi
if [[ "${1:-}" == integration ]]; then
  uv run --locked --with pyyaml==6.0.3 python scripts/mock_integration.py
  exit 0
fi
uv sync --locked --all-extras
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest --cov=. --cov-report=xml


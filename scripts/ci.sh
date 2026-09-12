#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "${1:-}" == security || "${1:-}" == bandit ]]; then
  uvx --from bandit==1.8.6 bandit -r . -lll -x .git,.venv,.venv-ci,tests,scripts/release.py,scripts/release_control.py
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


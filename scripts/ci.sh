#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Bandit can return zero after parser/plugin exceptions; validate its report too.
run_bandit() {
  uv run --no-build --no-project --python 3.12 \
    --with-requirements .github/requirements-bandit.txt python scripts/run_bandit.py
}

if [[ "${1:-}" == security || "${1:-}" == bandit ]]; then
  run_bandit
  if [[ "${1:-}" == security ]]; then
    command -v trivy >/dev/null || { echo 'Trivy is required for the complete local security gate.' >&2; exit 1; }
    trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --exit-code 1 --skip-dirs .git,.venv,.venv-ci,release-dist,dist,build .
  fi
  exit 0
fi
if [[ "${1:-}" == integration ]]; then
  uv run --no-build --locked --with pyyaml==6.0.3 python scripts/mock_integration.py
  exit 0
fi
uv sync --no-build --locked --all-extras
uv run --no-build --locked ruff check .
uv run --no-build --locked ruff format --check .
uv run --no-build --locked pytest tests .github/release-tests .github/workflow-tests --cov=. --cov-report=xml


#!/usr/bin/env bash
set -euo pipefail
ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
exec python3 "$ROOT/scripts/release.py" "$@"

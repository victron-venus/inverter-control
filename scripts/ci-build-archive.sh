#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="${PYTHON:-python3}"
output="${1:?usage: scripts/ci-build-archive.sh OUTPUT_DIR}"

worktree="$(mktemp -d "${TMPDIR:-/tmp}/inverter-control-archive.XXXXXX")"
rmdir "$worktree"
cleanup() {
  git -C "$root" worktree remove --force "$worktree" >/dev/null 2>&1 || rm -rf "$worktree"
}
trap cleanup EXIT

git -C "$root" worktree add --detach "$worktree" HEAD >/dev/null

base="$("$python_bin" - "$worktree" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "scripts"))
import version_plan

policy = json.loads((root / ".release-policy.json").read_text(encoding="utf-8"))
base = version_plan.read_base_version(root, policy)
source = subprocess.check_output(
    ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
).strip()
plan = version_plan.create_plan(base, "beta", 1, source, policy)
(root / ".release-plan.json").write_text(
    json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
version_plan.sync_versions(root, policy, plan)
print(base)
PY
)"

mkdir -p "$output"
"$python_bin" "$worktree/scripts/package_release.py" "$base" beta --output "$output"

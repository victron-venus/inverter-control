#!/usr/bin/env bash
# Bump version file, tag, push, GitHub release, then fetch so reruns see the new tag.
#
# Notes:
#   - release.txt (non-empty) must contain the release notes; it is cleared after a
#     successful release so the next release starts fresh.
#   - The test suite MUST pass before anything is tagged (we shipped broken releases
#     before; this is the safety gate).
#   - The GitHub release is created here via `gh`, and the Publish Release Assets
#     workflow may also create/update it on tag push (attaches the PackageManager
#     archive). Both are tolerated: an existing release is treated as OK.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NOTES_FILE="$SCRIPT_DIR/release.txt"

if [ ! -f "$NOTES_FILE" ]; then
    echo "Error: $NOTES_FILE not found — create release notes there."
    exit 1
fi

if [ ! -s "$NOTES_FILE" ]; then
    echo "Error: release.txt is empty"
    exit 1
fi

cd "$SCRIPT_DIR"

# --force: local tags may point at stale objects (e.g. after repo history
# imports) and a plain fetch would exit non-zero on "would clobber existing
# tag", aborting the script under set -e. Remote tags are authoritative.
echo ">>> git fetch origin --tags --force"
git fetch origin --tags --force

# Require a clean tree: no staged/unstaged changes AND no untracked files
# (git diff-index alone misses untracked files).
if ! git diff-index --quiet HEAD -- || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    echo "Error: uncommitted or untracked files. Commit, stash, or add them to .gitignore first."
    git status --short
    exit 1
fi

# Safety gate: never release broken code.
echo ">>> Running test suite..."
python3 -m pytest -q
echo "    Tests OK"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "main" ]]; then
    read -p "Not on main (on $BRANCH). Continue? [y/N] " -n 1 -r
    echo
    [[ ${REPLY:-} =~ ^[Yy]$ ]] || exit 1
fi

# Warn if local HEAD is not in sync with origin/main
LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
    echo "Warning: local HEAD differs from origin/main:"
    echo "  local : $LOCAL_HEAD"
    echo "  remote: $REMOTE_HEAD"
    read -p "Continue anyway? [y/N] " -n 1 -r
    echo
    [[ ${REPLY:-} =~ ^[Yy]$ ]] || exit 1
fi

# Highest semver tag (git describe can miss a newer tag not on the direct ancestry path)
LATEST_TAG=$(git tag -l 'v*' | sort -V | tail -n1)
if [ -z "$LATEST_TAG" ]; then
    LATEST_TAG="v0.0.0"
fi
echo "Latest tag (after fetch): $LATEST_TAG"

if [[ $LATEST_TAG =~ ^v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    MAJOR="${BASH_REMATCH[1]}"
    MINOR="${BASH_REMATCH[2]}"
    PATCH="${BASH_REMATCH[3]}"
    NEW_TAG="v$MAJOR.$MINOR.$((PATCH + 1))"
else
    NEW_TAG="v1.0.0"
fi

NEW_VER="${NEW_TAG#v}"

VERSION_FILE=""
if [ -f VERSION ]; then
    VERSION_FILE=VERSION
elif [ -f version ]; then
    VERSION_FILE=version
fi

# Do not release below committed VERSION (e.g. tags lagging behind a bumped VERSION file)
if [ -n "$VERSION_FILE" ]; then
    OLD=$(tr -d '\r\n' < "$VERSION_FILE")
    OLD_NUM="${OLD#v}"
    if [[ "$OLD_NUM" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ "$NEW_VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        HIGHER=$(printf '%s\n' "$NEW_VER" "$OLD_NUM" | sort -V | tail -n1)
        if [ "$HIGHER" != "$NEW_VER" ]; then
            echo ">>> $VERSION_FILE ($OLD_NUM) is ahead of tag-based bump ($NEW_VER); releasing $HIGHER instead (no downgrade)."
            NEW_VER="$HIGHER"
            NEW_TAG="v$NEW_VER"
        fi
    fi
fi

echo "Planned release: $NEW_TAG (semver digits: $NEW_VER)"

read -p "Proceed with release $NEW_TAG? [y/N] " -n 1 -r
echo
if [[ ! ${REPLY:-} =~ ^[Yy]$ ]]; then
    echo "Cancelled"
    exit 0
fi

if [ -n "$VERSION_FILE" ]; then
    OLD=$(tr -d '\r\n' < "$VERSION_FILE")
    if echo "$OLD" | grep -q '^v'; then
        NEW_CONTENT="v$NEW_VER"
    else
        NEW_CONTENT="$NEW_VER"
    fi
    if [ "$OLD" != "$NEW_CONTENT" ]; then
        printf '%s\n' "$NEW_CONTENT" > "$VERSION_FILE"
        git add "$VERSION_FILE"
        git commit -m "Bump $VERSION_FILE to $NEW_CONTENT for release $NEW_TAG"
    fi
fi

# Keep pyproject.toml in sync (CLAUDE.md three-way rule): the version file is
# read at runtime/dashboards, pyproject by setuptools/pip - drift shows a wrong
# package version. This is how pyproject stranded at 1.21.0 during v1.21.1.
PYPROJECT="$SCRIPT_DIR/pyproject.toml"
if [ -f "$PYPROJECT" ] && grep -q '^version[[:space:]]*=' "$PYPROJECT"; then
    python3 - "$PYPROJECT" "$NEW_VER" <<'PYEOF'
import re
import sys

path, ver = sys.argv[1], sys.argv[2]
src = open(path).read()
new = re.sub(r'(?m)^(version\s*=\s*)"[^"]*"', rf'\g<1>"{ver}"', src, count=1)
if new == src:
    sys.exit("ERROR: could not rewrite version in pyproject.toml")
open(path, "w").write(new)
print(f">>> pyproject.toml version -> {ver}")
PYEOF
    git add "$PYPROJECT"
    if ! git diff --cached --quiet; then
        git commit -m "Sync pyproject.toml to $NEW_VER for release $NEW_TAG"
    fi
fi

# Post-bump assertion: all three sources must agree before tagging.
if [ -n "$VERSION_FILE" ] && [ -f "$PYPROJECT" ]; then
    FILE_VER=$(tr -d '\r\nv' < "$VERSION_FILE")
    PROJ_VER=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PYPROJECT" | head -n1)
    if [ "$FILE_VER" != "$PROJ_VER" ]; then
        echo "Error: version mismatch after bump: $VERSION_FILE=$FILE_VER pyproject=$PROJ_VER"
        exit 1
    fi
fi

git tag -a "$NEW_TAG" -m "Release $NEW_TAG"

echo ">>> git push origin $BRANCH && git push origin $NEW_TAG"
git push origin "$BRANCH"
git push origin "$NEW_TAG"

echo ">>> gh release create"
if ! gh release create "$NEW_TAG" --title "$NEW_TAG" --notes-file "$NOTES_FILE"; then
    # The Publish Release Assets workflow may already have created the release
    # on tag push. Tolerate that instead of aborting mid-script.
    if gh release view "$NEW_TAG" >/dev/null 2>&1; then
        echo "    Release $NEW_TAG already exists (created by CI or a prior run) — skipping."
    else
        echo "Error: could not create release $NEW_TAG"
        exit 1
    fi
fi

echo ">>> git fetch origin --tags && git pull --ff-only"
git fetch origin --tags
git pull --ff-only origin "$BRANCH"

# Clear release notes so the next release starts fresh (the script requires a non-empty file).
: > "$NOTES_FILE"
echo ">>> Cleared release.txt — write fresh notes for the next release"

echo ">>> Done: $NEW_TAG"

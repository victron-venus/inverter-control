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

echo ">>> git fetch origin --tags"
git fetch origin --tags

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

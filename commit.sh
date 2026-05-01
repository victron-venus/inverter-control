#!/bin/bash
# Commit changes and create PR using message from commit.txt

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MSG_FILE="$SCRIPT_DIR/commit.txt"

if [ ! -f "$MSG_FILE" ]; then
  echo "Error: $MSG_FILE not found"
  echo "Create commit.txt with your commit message"
  exit 1
fi

if [ ! -s "$MSG_FILE" ]; then
  echo "Error: commit.txt is empty"
  exit 1
fi

cd "$SCRIPT_DIR"

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

if [ "$CURRENT_BRANCH" = "main" ]; then
  # Create feature branch from main
  TIMESTAMP=$(date +%Y%m%d_%H%M%S)
  SAFE_MSG=$(head -c 30 "$MSG_FILE" | tr '[:upper:]' '[:lower:]' | tr ' ' '_' | tr -cd '[:alnum:]_')
  BRANCH_NAME="feature/auto-${SAFE_MSG}"
  echo ">>> On main branch. Creating feature branch: $BRANCH_NAME..."
  git checkout -b "$BRANCH_NAME"
fi

echo ">>> Adding all changes..."
git add -A

echo ">>> Committing..."
git commit -F "$MSG_FILE"

if [ $? -ne 0 ]; then
  echo ">>> Nothing to commit or commit failed"
  exit 1
fi

echo ">>> Pushing branch..."
git push -u origin HEAD

if ! command -v gh &> /dev/null; then
  echo ">>> Warning: GitHub CLI (gh) not found. Install it or create PR manually:"
  echo "   PR needed: $(git config --get remote.origin.url)/pull/new/$(git rev-parse --abbrev-ref HEAD)"
  exit 0
fi

echo ">>> Creating PR with auto-merge label..."
gh pr create --title "$(head -1 "$MSG_FILE")" --body-file "$MSG_FILE" --label "auto-merge"
if [ $? -eq 0 ]; then
  echo ">>> PR created! Enabling auto-merge..."
  gh pr merge --auto --squash
  if [ $? -eq 0 ]; then
    echo ">>> Auto-merge enabled!"
  else
    echo ">>> Auto-merge enable failed"
  fi
else
  echo ">>> PR creation failed or already exists"
  # Try to enable auto-merge on existing PR
  BRANCH=$(git rev-parse --abbrev-ref HEAD)
  EXISTING_PR=$(gh pr list --head "$BRANCH" --json url --jq '.[0].url' 2>/dev/null)
  if [ -n "$EXISTING_PR" ]; then
    echo ">>> Enabling auto-merge on existing PR..."
    gh pr merge --auto --squash "$EXISTING_PR"
  fi
fi

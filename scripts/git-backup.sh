#!/usr/bin/env bash
set -euo pipefail

repo_dir="${REPO_DIR:-$(git rev-parse --show-toplevel)}"
branch="${GITHUB_BACKUP_BRANCH:-backups/agent}"
cd "$repo_dir"
git add --all
if git diff --cached --quiet; then
  echo "No changes to back up."
  exit 0
fi
git commit -m "chore(company-ops): automated backup"
git push origin "HEAD:$branch"

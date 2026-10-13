#!/usr/bin/env bash
# Land a close_work `learned_for_agents` text in the git-tracked company memory.
#   scripts/capture-learning.sh <decisions|learnings|playbooks|research|experiments|hypotheses> <slug> <steward-task-id> "<title>" < body.md
# Body comes from stdin (paste the same text passed to close_work learned_for_agents).
# Writes memory/<category>/<YYYY-MM-DD>-<slug>.md; does NOT commit (the manager commits it with the ticket).
set -euo pipefail
[ $# -eq 4 ] || { sed -n '2,5p' "$0" >&2; exit 2; }
category=$1; slug=$2; task=$3; title=$4
root=$(cd "$(dirname "$0")/.." && pwd)
case "$category" in decisions|learnings|playbooks|research|experiments|hypotheses) ;; *) echo "bad category: $category" >&2; exit 2;; esac
[[ "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "slug must be kebab-case" >&2; exit 2; }
body=$(cat); [ -n "$body" ] || { echo "empty body on stdin" >&2; exit 2; }
if grep -Eqi '(api[_-]?key|secret|token|password|BEGIN [A-Z ]*PRIVATE KEY)[[:space:]]*[:=]' <<<"$body"; then echo "refusing: body looks like it contains a credential" >&2; exit 3; fi
out="$root/memory/$category/$(date -u +%F)-$slug.md"
[ ! -e "$out" ] || { echo "exists: $out" >&2; exit 4; }
printf -- '---\nsteward_task: %s\ndate: %s\n---\n# %s\n\n%s\n' "$task" "$(date -u +%F)" "$title" "$body" > "$out"
echo "$out"

#!/usr/bin/env bash
set -euo pipefail

config_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
project_dir=$(cd "$config_dir/.." && pwd)

link_if_missing() {
  local source=$1 target=$2
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    ln -s "$source" "$target"
    echo "linked $target"
  else
    echo "kept $target"
  fi
}

link_repo() {
  local repo=$1
  [[ -d "$repo" ]] || { echo "missing repo: $repo" >&2; return 1; }
  link_if_missing "../company-os/agent-config/CLAUDE.md" "$repo/CLAUDE.md"
  link_if_missing "../company-os/agent-config/AGENTS.md" "$repo/AGENTS.md"
  link_if_missing "../company-os/agent-config/opencode.json" "$repo/opencode.json"
  mkdir -p "$repo/.codex"
  link_if_missing "../../company-os/agent-config/codex-config.json" "$repo/.codex/config.json"
}

if [[ $# -eq 0 ]]; then
  link_repo "$project_dir"
else
  for repo in "$@"; do link_repo "$repo"; done
fi

#!/usr/bin/env bash
# Entrypoint for the reviewer pod (Dockerfile.reviewer). Deliberately NOT the engineering
# entrypoint: no Infisical bootstrap identity, no generated MCP config (no Steward/kubernetes/neon
# servers), no repo sync. Credentials arrive only as env from the Secret reviewer-secrets
# (synced from Infisical /reviewer).
set -euo pipefail
export HOME="${HOME:-/home/reviewer}"

mkdir -p "$HOME/.config/opencode"
cp /opt/reviewer/opencode.json "$HOME/.config/opencode/opencode.json"
cp /opt/reviewer/AGENTS.md "$HOME/.config/opencode/AGENTS.md"
cp /opt/reviewer/AGENTS.md /workspace/AGENTS.md

# The agent is told which model to use; refuse the silent Claude fallback.
case "${RUNNER_MODEL:-}" in
  oc-*) ;;
  *) echo "[reviewer] RUNNER_MODEL must be oc-<provider>/<model> (got '${RUNNER_MODEL:-}')" >&2; exit 1 ;;
esac

# Hive registration declares github-merge only when the three App credentials are present.
exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- node /opt/company-ops/scripts/engineering-manager-mcp.js

#!/usr/bin/env bash
# Entrypoint for the reviewer pod (Dockerfile.reviewer). Deliberately NOT the engineering
# entrypoint: no Infisical bootstrap identity, no generated MCP config (no Steward/kubernetes/neon
# servers), no repo sync. Credentials arrive only as env: the three GitHub App keys
# from company-ops-secrets (same App as engineering).
set -euo pipefail
export HOME="${HOME:-/home/reviewer}"

cp /opt/reviewer/AGENTS.md /workspace/CLAUDE.md

# The agent is told which model to use. Claude runs need the OAuth token; OpenCode models must
# carry the oc- prefix or ai-cli silently runs them on Claude.
case "${RUNNER_AGENT:-}" in
  claude) [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] || { echo "[reviewer] RUNNER_AGENT=claude needs CLAUDE_CODE_OAUTH_TOKEN" >&2; exit 1; } ;;
  *) case "${RUNNER_MODEL:-}" in
       oc-*) ;;
       *) echo "[reviewer] RUNNER_MODEL must be oc-<provider>/<model> (got '${RUNNER_MODEL:-}')" >&2; exit 1 ;;
     esac ;;
esac

# Hive registration declares github-merge only when the three App credentials are present.
exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- node /opt/company-ops/scripts/engineering-manager-mcp.js

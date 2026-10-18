#!/usr/bin/env bash
# Entrypoint for the reviewer pod (Dockerfile.reviewer). Deliberately NOT the engineering
# entrypoint: no Infisical bootstrap identity, no generated MCP config (no Steward/kubernetes/neon
# servers), no repo sync. Credentials arrive only as env: the three GitHub App keys
# from company-ops-secrets (same App as engineering).
set -euo pipefail
export HOME="${HOME:-/home/reviewer}"

# Claude reads CLAUDE.md; the OpenCode fallbacks read the same rules plus the locked-down config.
mkdir -p "$HOME/.config/opencode"
cp /opt/reviewer/opencode.json "$HOME/.config/opencode/opencode.json"
cp /opt/reviewer/AGENTS.md "$HOME/.config/opencode/AGENTS.md"
cp /opt/reviewer/AGENTS.md /workspace/AGENTS.md
cp /opt/reviewer/AGENTS.md /workspace/CLAUDE.md

# Primary runner is Claude and needs the OAuth token. Fallback models must carry the oc- prefix
# or ai-cli silently runs them on Claude.
[[ "${RUNNER_AGENT:-}" == "claude" ]] || { echo "[reviewer] RUNNER_AGENT must be claude (got '${RUNNER_AGENT:-}')" >&2; exit 1; }
[[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] || { echo "[reviewer] RUNNER_AGENT=claude needs CLAUDE_CODE_OAUTH_TOKEN" >&2; exit 1; }
IFS=',' read -ra fallbacks <<< "${RUNNER_FALLBACK_MODELS:-}"
for m in "${fallbacks[@]}"; do
  [[ -z "$m" || "$m" == oc-* ]] || { echo "[reviewer] fallback '$m' must be oc-<provider>/<model>" >&2; exit 1; }
done

# Hive registration declares github-merge only when the three App credentials are present.
exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- node /opt/company-ops/scripts/engineering-manager-mcp.js

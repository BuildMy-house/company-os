#!/usr/bin/env bash
# Entrypoint for the marketing-lead pod's main container (Dockerfile.marketing-lead).
# Deliberately NOT the engineering entrypoint: no Infisical bootstrap identity, no generated
# MCP config (no Steward/kubernetes/neon servers), no repo sync. Credentials arrive only as
# env from the Secret marketing-lead-secrets (synced from Infisical /marketing-lead).
set -euo pipefail
export HOME="${HOME:-/home/marketing-lead}"

mkdir -p "$HOME/.config/opencode/skills" /workspace/memory /var/lib/marketing-lead
cp /opt/marketing-lead/opencode.json "$HOME/.config/opencode/opencode.json"
cp /opt/marketing-lead/AGENTS.md "$HOME/.config/opencode/AGENTS.md"
cp /opt/marketing-lead/AGENTS.md /workspace/AGENTS.md
cp /opt/marketing-lead/skills/*.md "$HOME/.config/opencode/skills/"
# Seed topic memory once; never overwrite what the agent has since changed.
cp -rn /opt/marketing-lead/memory/. /workspace/memory/

# The agent is told which model to use; refuse the silent Claude fallback.
case "${RUNNER_MODEL:-}" in
  oc-*) ;;
  *) echo "[marketing-lead] RUNNER_MODEL must be oc-<provider>/<model> (got '${RUNNER_MODEL:-}')" >&2; exit 1 ;;
esac

# Hive registration declares tools once, at startup. The sidecar writes its ready markers only
# after its DB/env checks pass, so give it up to 60s; a pod whose sidecar never becomes ready
# simply starts without marketing-ingest / substack-publish (restart the pod after fixing it).
if [ -n "${MARKETING_INGEST_READY_FILE:-}" ]; then
  for _ in $(seq 1 60); do [ -e "$MARKETING_INGEST_READY_FILE" ] && break; sleep 1; done
  [ -e "$MARKETING_INGEST_READY_FILE" ] || echo "[marketing-lead] sidecar not ready; marketing-ingest and substack-publish will not be declared" >&2
fi

exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- node /opt/company-ops/scripts/engineering-manager-mcp.js

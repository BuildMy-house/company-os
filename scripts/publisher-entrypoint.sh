#!/usr/bin/env bash
# Entrypoint for the publisher pod's main container (Dockerfile.publisher).
# Deliberately NOT the engineering entrypoint: no Infisical bootstrap identity, no generated
# MCP config (no Steward/kubernetes/neon servers), no repo sync. Credentials arrive only as
# env from the Secret publisher-secrets (synced from Infisical /publisher).
set -euo pipefail
export HOME="${HOME:-/home/publisher}"

mkdir -p "$HOME/.config/opencode/skills" /workspace/memory /var/lib/publisher
cp /opt/publisher/opencode.json "$HOME/.config/opencode/opencode.json"
cp /opt/publisher/AGENTS.md "$HOME/.config/opencode/AGENTS.md"
cp /opt/publisher/AGENTS.md /workspace/AGENTS.md
cp /opt/publisher/skills/*.md "$HOME/.config/opencode/skills/"
# Seed topic memory once; never overwrite what the agent has since changed.
cp -rn /opt/publisher/memory/. /workspace/memory/

# The agent is told which model to use; refuse the silent Claude fallback.
case "${RUNNER_MODEL:-}" in
  oc-*) ;;
  *) echo "[publisher] RUNNER_MODEL must be oc-<provider>/<model> (got '${RUNNER_MODEL:-}')" >&2; exit 1 ;;
esac

# Hive registration declares tools once, at startup. The sidecar writes its ready markers only
# after its DB/env checks pass, so give it up to 60s; a pod whose sidecar never becomes ready
# simply starts without marketing-ingest / substack-publish (restart the pod after fixing it).
if [ -n "${MARKETING_INGEST_READY_FILE:-}" ]; then
  for _ in $(seq 1 60); do [ -e "$MARKETING_INGEST_READY_FILE" ] && break; sleep 1; done
  [ -e "$MARKETING_INGEST_READY_FILE" ] || echo "[publisher] sidecar not ready; marketing-ingest and substack-publish will not be declared" >&2
fi

exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- node /opt/company-ops/scripts/engineering-manager-mcp.js

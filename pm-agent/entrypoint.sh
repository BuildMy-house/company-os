#!/usr/bin/env bash
# pm-agent container entrypoint: Hermes runtime (loopback) + front door.
# Unlike hermes/scripts/entrypoint.sh this does NOT fetch from Infisical, sync
# repositories, or write a broad .env: the pod's only secrets are the two keys
# in pm-agent-secrets, delivered as env vars.
set -euo pipefail

: "${PM_DATABASE_URL:?PM_DATABASE_URL required (pm-agent-secrets)}"
: "${PM_AGENT_LLM_API_KEY:?PM_AGENT_LLM_API_KEY required (pm-agent-secrets)}"

export HERMES_HOME="${HERMES_HOME:-/home/pmagent/.hermes}"
mkdir -p "$HERMES_HOME" /run/pm-agent
cp /opt/company-ops/pm-agent/hermes-config.yaml "$HERMES_HOME/config.yaml"
# Per-project persona: forks set PM_AGENT_CONTENT_DIR; default is the
# buildmy.house content baked into the image (matches guard.py's default).
cp "${PM_AGENT_CONTENT_DIR:-/opt/company-ops/pm-agent/content/buildmy-house}/SOUL.md" "$HERMES_HOME/SOUL.md"

# Per-boot loopback credential between the front door and the Hermes runtime.
API_SERVER_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
export API_SERVER_KEY

hermes gateway run &
HERMES_PID=$!
python /opt/company-ops/pm-agent/agent.py &
AGENT_PID=$!

# If either process dies the pod must restart: fail the whole container.
wait -n "$HERMES_PID" "$AGENT_PID"
status=$?
kill "$HERMES_PID" "$AGENT_PID" 2>/dev/null || true
exit "$status"

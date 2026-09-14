#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Infisical Secrets Injection ─────────────────────────────────────────────
# Requires INFISICAL_UNIVERSAL_AUTH_CLIENT_ID/SECRET (+ HOST_URL/PROJECT_ID).
# Fetches every secret in the project/environment and injects them into the
# environment. Actual credentials (AXIOM_TOKEN, GITHUB_APP_*, etc.) live only
# in Infisical, never in .env or git — only the bootstrap identity does.
if [ -n "${INFISICAL_UNIVERSAL_AUTH_CLIENT_ID:-}" ]; then
  echo "[infisical] Fetching secrets from Infisical (${INFISICAL_ENV:-dev})..."
  SECRETS_FILE=$(mktemp)
  trap "rm -f $SECRETS_FILE" EXIT

  if node "$SCRIPT_DIR/fetch-infisical-secrets.js" > "$SECRETS_FILE"; then
    echo "[infisical] Secrets loaded successfully"
    set -a
    source "$SECRETS_FILE"
    set +a
  else
    echo "[infisical] ERROR: Failed to fetch secrets from Infisical" >&2
    exit 1
  fi
else
  echo "[infisical] INFISICAL_UNIVERSAL_AUTH_CLIENT_ID not set; using .env or existing environment variables"
fi

# ── Claude Code telemetry -> Axiom ──────────────────────────────────────────
# Native OTLP metrics+logs export (official Claude Code feature, no plugin
# needed) — token/cost counters and tool accept/reject counts, not full
# trace content. ai-cli-mcp spawns `claude` as a child of this shell, so
# these env vars propagate to every dispatched Claude Code run automatically.
if [ -n "${AXIOM_TOKEN:-}" ]; then
  export CLAUDE_CODE_ENABLE_TELEMETRY=1
  export OTEL_METRICS_EXPORTER=otlp
  export OTEL_LOGS_EXPORTER=otlp
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.axiom.co
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ${AXIOM_TOKEN},X-Axiom-Dataset=bmh-company"
  export OTEL_RESOURCE_ATTRIBUTES="service.name=claude-code,deployment.environment.name=production"
  echo "[otel] Claude Code telemetry -> Axiom (bmh-company)"
fi

# ── Agent MCP Configuration (Axiom, Infisical, Neon, Cloudflare, Steward, ai-cli) ──
# Gives claude and opencode the same MCP server list, wired from whatever
# credentials are present in the container's environment, so any repo the
# container works in gets the same tool access regardless of that repo's
# own committed config.
echo "[mcp-config] Generating agent MCP config for Claude and OpenCode..."
node "$SCRIPT_DIR/generate-agent-mcp-config.js"

if [ -n "${AXIOM_TOKEN:-}" ]; then
  codex mcp add axiom \
    --env AXIOM_TOKEN="$AXIOM_TOKEN" \
    --env AXIOM_ORG_ID="${AXIOM_ORG_ID:-}" \
    --env AXIOM_URL="${AXIOM_ENDPOINT:-https://api.axiom.co}" \
    -- npx -y mcp-server-axiom >/dev/null 2>&1 \
    && echo "[mcp-config] wrote axiom to codex" \
    || echo "[mcp-config] WARN: codex mcp add axiom failed" >&2
fi

if [ -n "${INFISICAL_UNIVERSAL_AUTH_CLIENT_ID:-}" ]; then
  codex mcp add infisical \
    --env INFISICAL_HOST_URL="${INFISICAL_HOST_URL:-https://app.infisical.com}" \
    --env INFISICAL_AUTH_METHOD=universal-auth \
    --env INFISICAL_UNIVERSAL_AUTH_CLIENT_ID="$INFISICAL_UNIVERSAL_AUTH_CLIENT_ID" \
    --env INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET="$INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET" \
    -- npx -y --legacy-peer-deps @infisical/mcp >/dev/null 2>&1 \
    && echo "[mcp-config] wrote infisical to codex" \
    || echo "[mcp-config] WARN: codex mcp add infisical failed" >&2
fi

# ── AI-CLI-MCP Configuration ────────────────────────────────────────
echo "[ai-cli] Setting up ai-cli-mcp configuration..."
mkdir -p /root/.config/ai-cli

# Create config if not already present
if [[ ! -f /root/.config/ai-cli/config.toml ]]; then
  cat > /root/.config/ai-cli/config.toml <<'AICLI_EOF'
[worker.free]
agent = "opencode"
model = "tokenrouter/z-ai/glm-5.3-free"
timeout_seconds = 300
description = "TokenRouter — FREE, no cost. Default worker; exploit as heavily as it can handle before escalating to a paid/quota-limited tier."

[worker.cheap]
agent = "opencode"
model = "oc-opencode/big-pickle"
timeout_seconds = 300
description = "Free tier, default for mechanical tasks"

[worker.balanced]
agent = "opencode"
model = "oc-opencode/mimo-v2.5-free"
timeout_seconds = 300
description = "Free tier, can stall on 30-50+ tool calls"

[worker.hard]
agent = "claude"
model = "opus"
timeout_seconds = 900
description = "Claude Opus, highest capability"

[worker.quick]
agent = "opencode"
model = "oc-opencode/nemotron-3-ultra-free"
timeout_seconds = 180
description = "Free tier, fast alternative"

[default]
worker = "free"
mcp_server_port = 3001
logging_level = "info"
AICLI_EOF
  echo "[ai-cli] Created default config at /root/.config/ai-cli/config.toml"
fi

# Export environment variables for ai-cli
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  export ANTHROPIC_API_KEY
  echo "[ai-cli] ANTHROPIC_API_KEY set"
elif [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  export CLAUDE_CODE_OAUTH_TOKEN
  echo "[ai-cli] CLAUDE_CODE_OAUTH_TOKEN set (subscription auth)"
fi

# Codex auth: CODEX_API_KEY is read directly by the CLI, but OAuth needs an
# actual `codex login` run to persist ~/.codex/auth.json — the CLI does not
# read an OAuth token from the environment per-invocation the way `claude` does.
if [[ -n "${CODEX_API_KEY:-}" ]]; then
  export CODEX_API_KEY
  echo "[ai-cli] CODEX_API_KEY set"
elif [[ -n "${CODEX_ACCESS_TOKEN:-}" ]]; then
  if printf '%s' "$CODEX_ACCESS_TOKEN" | codex login --with-access-token >/dev/null 2>&1; then
    echo "[ai-cli] Codex OAuth session established via CODEX_ACCESS_TOKEN"
  else
    echo "WARN: codex login --with-access-token failed" >&2
  fi
fi

if [[ -n "${OPENCODE_API_KEY:-}" ]]; then
  export OPENCODE_API_KEY
  echo "[ai-cli] OPENCODE_API_KEY set"
fi
if [[ -n "${OPENCODE_GO_API_KEY:-}" ]]; then
  export OPENCODE_GO_API_KEY
  echo "[ai-cli] OPENCODE_GO_API_KEY set"
fi
if [[ -n "${ZAI_CODING_PLAN_API_KEY:-}" ]]; then
  export ZAI_CODING_PLAN_API_KEY
  echo "[ai-cli] ZAI_CODING_PLAN_API_KEY set"
fi
if [[ -n "${TOKENROUTER_API_KEY:-}" ]]; then
  export TOKENROUTER_API_KEY
  echo "[ai-cli] TOKENROUTER_API_KEY set (free tier — default worker)"
fi

echo "[ai-cli] ai-cli-mcp configured and ready"

# ── Repo checkouts are on-demand, not pre-cloned ────────────────────────
# Cloning all 5 repos eagerly on every boot slowed startup and meant every
# task paid for repos it never touched, on top of sharing one fixed
# checkout per repo across every task the container ever runs (a collision
# risk if two tasks touch the same repo concurrently). scripts/sync-repo.sh
# does the actual clone/fetch on demand instead — surface the convention
# where whichever agent starts up will see it.
mkdir -p /workspace
cat > /workspace/README.md <<'EOF'
# Repo checkouts are on-demand

The BuildMy-house repos are not pre-cloned at container startup. Before
working in one you haven't synced yet this session, run:

    bash /opt/company-ops/scripts/sync-repo.sh <name>

where <name> is one of: app, website, company-os, hermees, observer-website.

This mints a fresh GitHub App installation token and clones (first run) or
fetches + hard-resets (later runs) into /workspace/<name>-checkout. Safe to
re-run any time you want the latest remote state.
EOF

exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- npx -y ai-cli-mcp@latest

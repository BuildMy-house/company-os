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
  # Unset OTEL_TRACES_EXPORTER defaults to "otlp" per spec whenever an
  # endpoint is configured. ai-cli-mcp spawns opencode as a child of this
  # shell, so opencode inherits these vars and — via its own
  # @effect/opentelemetry instrumentation — was exporting full internal
  # trace spans (e.g. "SQLiteDrizzle.make", nearly all fields null) to
  # bmh-company. That dataset is content-light by design (model/tokens/
  # tool-names only, via axiom-usage.js and hermes' axiom_usage plugin);
  # disable traces explicitly so neither Claude Code nor opencode leaks them.
  export OTEL_TRACES_EXPORTER=none
  export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
  export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.axiom.co
  export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ${AXIOM_TOKEN},X-Axiom-Dataset=bmh-company"
  export OTEL_RESOURCE_ATTRIBUTES="service.name=${AXIOM_SERVICE_NAME:-claude-code},deployment.environment.name=${DEPLOYMENT_ENVIRONMENT:-production},role=manager"
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
# No named worker aliases (free/cheap/balanced/quick/flash/hard) — the
# engineering-manager dispatches with an explicit `--model <provider/model>`
# on every call (informed by the model-routing Steward memory and a live
# `ai-cli models` listing, see .agents/agent-manager.md), so a preset tier
# table only added a second, drifting source of truth to keep in sync.
echo "[ai-cli] Setting up ai-cli-mcp configuration..."
mkdir -p "$HOME/.config/ai-cli"
mkdir -p "$HOME/.claude"
cp /opt/company-ops/.claude/agents/agent-manager.md "$HOME/.claude/CLAUDE.md"

# Create config if not already present
if [[ ! -f "$HOME/.config/ai-cli/config.toml" ]]; then
  cat > "$HOME/.config/ai-cli/config.toml" <<'AICLI_EOF'
[default]
mcp_server_port = 3001
logging_level = "info"

[dispatch]
stall_detector_enabled = true
stall_watch_timeout_sec = 600
hard_cap_sec = 1800
AICLI_EOF
  echo "[ai-cli] Created default config at $HOME/.config/ai-cli/config.toml"
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
  if [[ "$(id -u)" != "0" ]]; then
    echo "[ai-cli] Skipping Codex OAuth file login for non-root worker; use CODEX_API_KEY or a writable Codex home"
  elif printf '%s' "$CODEX_ACCESS_TOKEN" | codex login --with-access-token >/dev/null 2>&1; then
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
mkdir -p /workspace/{app,website,company-os,hermees,observer-website}-checkout
cp /opt/company-ops/workspace-root/AGENTS.md /workspace/AGENTS.md
cp /opt/company-ops/workspace-root/AGENTS_STEWARD.md /workspace/AGENTS_STEWARD.md
cat > /workspace/README.md <<'EOF'
# Repo checkouts are on-demand

Read `/workspace/AGENTS.md` first — it covers concerns that span every
checkout (Steward identity resolution, credentials, concurrent-session
safety, the manager pattern, model selection). `/workspace/AGENTS_STEWARD.md`
explains why this level has no `Repo:` identity of its own.

The BuildMy-house repos are not pre-cloned at container startup. Before
working in one you haven't synced yet this session, run:

    bash /opt/company-ops/scripts/sync-repo.sh <name>

where <name> is one of: app, website, company-os, hermees, observer-website.

This mints a fresh GitHub App installation token and clones (first run) or
fetches + hard-resets (later runs) into /workspace/<name>-checkout. Safe to
re-run any time you want the latest remote state.
EOF

exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- node /opt/company-ops/scripts/engineering-manager-mcp.js

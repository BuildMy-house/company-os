#!/usr/bin/env bash
set -e

# ── Infisical Secrets Injection ─────────────────────────────────────────────
# If INFISICAL_TOKEN is set, fetch secrets from Infisical workspace and inject
# them into the environment. Otherwise, proceed with .env or existing env vars.
if [ -n "${INFISICAL_TOKEN:-}" ]; then
  echo "[infisical] Fetching secrets from Infisical workspace..."
  SECRETS_FILE=$(mktemp)
  trap "rm -f $SECRETS_FILE" EXIT

  if infisical export --token "$INFISICAL_TOKEN" > "$SECRETS_FILE" 2>/dev/null; then
    echo "[infisical] Secrets loaded successfully"
    set -a
    source "$SECRETS_FILE"
    set +a
  else
    echo "[infisical] ERROR: Failed to fetch secrets from Infisical" >&2
    exit 1
  fi
else
  echo "[infisical] INFISICAL_TOKEN not set; using .env or existing environment variables"
fi

# ── AI-CLI-MCP Configuration ────────────────────────────────────────
echo "[ai-cli] Setting up ai-cli-mcp configuration..."
mkdir -p /root/.config/ai-cli

# Create config if not already present
if [[ ! -f /root/.config/ai-cli/config.toml ]]; then
  cat > /root/.config/ai-cli/config.toml <<'AICLI_EOF'
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
worker = "cheap"
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

echo "[ai-cli] ai-cli-mcp configured and ready"

# ── Agent MCP Configuration (Axiom, Infisical, Neon, Cloudflare, ai-cli) ──
# Gives claude and opencode the same MCP server list, wired from whatever
# credentials are present in the container's environment.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[mcp-config] Generating agent MCP config for Claude and OpenCode..."
node "$SCRIPT_DIR/generate-agent-mcp-config.js"

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

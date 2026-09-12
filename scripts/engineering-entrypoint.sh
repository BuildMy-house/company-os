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

# ── Agent MCP Configuration (Axiom, Infisical) ──────────────────────────────
# Wires the fetched credentials into user-scope MCP config for every agent
# (Claude, OpenCode, Codex), so any repo the container works in gets the same
# tool access regardless of that repo's own committed config.
echo "[mcp-config] Generating axiom/infisical MCP config for Claude and OpenCode..."
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
fi
if [[ -n "${CODEX_API_KEY:-}" ]]; then
  export CODEX_API_KEY
  echo "[ai-cli] CODEX_API_KEY set"
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

# ── Mint GitHub App installation token ──────────────────────────────────
GITHUB_TOKEN=""
if [[ -f "$SCRIPT_DIR/github-app-token.js" ]]; then
  GITHUB_TOKEN=$(node "$SCRIPT_DIR/github-app-token.js" 2>/dev/null || true)
fi

if [[ -n "$GITHUB_TOKEN" ]]; then
  echo "INFO: GitHub App installation token acquired successfully (ghs_****)"
else
  echo "WARN: Could not acquire GitHub App installation token; falling back to unauthenticated sync" >&2
fi

# ── Helper: mask tokens in output ───────────────────────────────────────
mask_tokens() {
  sed -E 's#x-access-token:[^@]+@#x-access-token:ghs_****@#g'
}

# ── URL resolution ──────────────────────────────────────────────────────
get_repo_url() {
  local name="$1"
  local env_val="$2"

  if [[ -n "$GITHUB_TOKEN" ]]; then
    echo "https://x-access-token:${GITHUB_TOKEN}@github.com/BuildMy-house/${name}.git"
  elif [[ -n "$env_val" && "$env_val" != git@* ]]; then
    echo "$env_val"
  else
    echo "https://github.com/BuildMy-house/${name}.git"
  fi
}

sync_repo() {
  local name="$1" raw_url="$2" path="$3"
  local url
  url=$(get_repo_url "$name" "$raw_url")
  local attempt max_attempts=3 delay=2 timeout_secs=30
  local masked_url
  masked_url=$(echo "$url" | mask_tokens)

  for (( attempt = 1; attempt <= max_attempts; attempt++ )); do
    local out="" status=0
    if [[ ! -d "$path/.git" ]]; then
      rm -rf "$path" 2>/dev/null
      out=$(timeout "${timeout_secs}s" git clone "$url" "$path" 2>&1) || status=$?
    else
      git -C "$path" remote set-url origin "$url" 2>/dev/null || true
      out=$(timeout "${timeout_secs}s" git -C "$path" fetch origin 2>&1) || status=$?
      if [[ $status -eq 0 ]]; then
        git -C "$path" reset --hard origin/HEAD 2>/dev/null || true
      fi
    fi

    if [[ -n "$out" ]]; then
      echo "$out" | mask_tokens
    fi

    if [[ $status -eq 0 ]]; then
      echo "PASS: $name sync succeeded ($masked_url)"
      return 0
    fi

    echo "WARN: $name sync failed (attempt $attempt/$max_attempts)" >&2
    (( attempt < max_attempts )) && sleep "$delay"
  done
  echo "ERROR: $name failed after $max_attempts attempts — $masked_url" >&2
  return 1
}

for repo in \
  "app:APP_REPO_URL:/workspace/app-checkout" \
  "website:WEBSITE_REPO_URL:/workspace/website-checkout" \
  "company-os:COMPANY_OS_REPO_URL:/workspace/company-os-checkout" \
  "hermees:HERMEES_REPO_URL:/workspace/hermees-checkout" \
  "observer-website:OBSERVER_WEBSITE_REPO_URL:/workspace/observer-website-checkout"; do
  IFS=: read -r name url_var path <<< "$repo"
  sync_repo "$name" "${!url_var:-}" "$path" || true
done

exec npx -y mcp-proxy --host 0.0.0.0 --port 8000 -- npx -y ai-cli-mcp@latest

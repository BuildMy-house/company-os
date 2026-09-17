#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Infisical Secrets Injection ─────────────────────────────────────────────
# Requires INFISICAL_UNIVERSAL_AUTH_CLIENT_ID/SECRET (+ HOST_URL/PROJECT_ID).
# Fetches every secret in the project/environment and injects them into the
# environment. Actual credentials (AXIOM_TOKEN, POSTGRES_PASSWORD, etc.) live
# only in Infisical, never in .env or git — only the bootstrap identity does.
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

chmod -R a+rwx /opt/data 2>/dev/null || true

# Keep Hermes built-in memory private and on the persistent Hermes volume.
mkdir -p /opt/data/memories
for memory_file in MEMORY.md USER.md; do
  if [[ ! -f "/opt/data/memories/$memory_file" && -f "/opt/company-ops/hermes/memories/$memory_file" ]]; then
    cp "/opt/company-ops/hermes/memories/$memory_file" "/opt/data/memories/$memory_file"
  fi
done

# Sync config.yaml + SOUL.md from package into mounted volume (first-volume-only fix)
#
# SOUL.md goes to $HERMES_HOME, NOT /root/.hermes — hermes-agent's own
# get_default_hermes_root() docstring: "~/.hermes, or HERMES_HOME itself in
# Docker/custom deployments (e.g. /opt/data)". This container sets
# HERMES_HOME=/opt/data, so /root/.hermes/SOUL.md was never read by hermes
# at all. Confirmed live: /opt/data/SOUL.md held hermes-agent's own
# auto-seeded generic default persona (667 bytes, "You are Hermes Agent,
# built by Nous Research" — no mention of buildmy.house, the Board, or any
# engineering surface) the entire time this container has been running,
# while our real SOUL.md sat unread at the old wrong path.
if [ -f /opt/company-ops/hermes/config.yaml ]; then
  cp /opt/company-ops/hermes/config.yaml /opt/data/config.yaml
fi

# SOUL.md: prefer a live override from the hermees-memory repo over the copy
# baked into the image, so day-to-day operational guidance (which agent to
# dispatch with, current known-broken tools, etc.) can be updated by pushing
# to hermees-memory + restarting this pod — no image rebuild required. Only
# rarer, structural changes to Hermes's actual identity/mandate still need a
# real rebuild. Falls back to the baked-in copy if the memory repo isn't
# reachable or has no override yet (e.g. first-ever boot). hermes-agent's own
# memory plugin also clones hermees-memory, but only after `hermes` itself
# starts (a few seconds into `exec hermes`, confirmed via /opt/hermees-memory's
# birth time vs PID 1's start time) — too late to matter here, so this clones
# its own copy early instead of waiting on/reusing that one.
MEMORY_REPO_DIR="/opt/hermees-memory"
MEMORY_TOKEN=""
# github-app-token.js only reads the private key from a file (checked paths
# include /etc/github/buildmyhouse-engineering-app.pem), never from the raw
# GITHUB_APP_PRIVATE_KEY env var directly. The engineering container gets
# this file written for it as a side effect of ai-cli-mcp's own setup; this
# container runs `hermes gateway run` instead, which never does that step —
# so without this, minting silently fails every time (caught live: the very
# first deploy of this feature logged "could not mint GitHub App token" on
# every boot). Write it ourselves from the env var if the file isn't already
# there.
if [ -n "${GITHUB_APP_PRIVATE_KEY:-}" ] && [ ! -f /etc/github/buildmyhouse-engineering-app.pem ]; then
  mkdir -p /etc/github
  printf '%s\n' "$GITHUB_APP_PRIVATE_KEY" > /etc/github/buildmyhouse-engineering-app.pem
  chmod 600 /etc/github/buildmyhouse-engineering-app.pem
fi
if [ -f "$SCRIPT_DIR/github-app-token.js" ]; then
  MEMORY_TOKEN=$(node "$SCRIPT_DIR/github-app-token.js" 2>/dev/null || true)
fi
if [ -n "$MEMORY_TOKEN" ]; then
  MEMORY_URL="https://x-access-token:${MEMORY_TOKEN}@github.com/BuildMy-house/hermees-memory.git"
  if [ -d "$MEMORY_REPO_DIR/.git" ]; then
    git -C "$MEMORY_REPO_DIR" remote set-url origin "$MEMORY_URL" 2>/dev/null || true
    git -C "$MEMORY_REPO_DIR" pull --ff-only 2>/dev/null || true
  else
    git clone "$MEMORY_URL" "$MEMORY_REPO_DIR" 2>/dev/null || true
  fi
else
  echo "[soul] WARN: could not mint GitHub App token; skipping hermees-memory pull, using baked-in SOUL.md" >&2
fi

if [ -f "$MEMORY_REPO_DIR/hermes/SOUL.md" ]; then
  echo "[soul] Using SOUL.md override from hermees-memory (no rebuild needed to update this)"
  cp "$MEMORY_REPO_DIR/hermes/SOUL.md" "${HERMES_HOME:-/opt/data}/SOUL.md"
elif [ -f /opt/company-ops/hermes/SOUL.md ]; then
  echo "[soul] No override in hermees-memory yet; using SOUL.md baked into the image"
  cp /opt/company-ops/hermes/SOUL.md "${HERMES_HOME:-/opt/data}/SOUL.md"
fi

# Write secrets from environment to /opt/data/.env (hermes reads this at startup)
cat > /opt/data/.env << 'ENVEOF'
# Auto-generated from container environment — do not edit manually
ENVEOF

# DISCORD_BOT_TOKEN/ALLOWED_USERS/ALLOWED_CHANNELS/ALLOW_ALL_USERS/HOME_CHANNEL
# are read natively by hermes-agent's own gateway (no custom bridge script
# needed — see gateway/config_env.py + plugins/platforms/discord/adapter.py
# in the installed hermes-agent package for the full env var surface).
# HIL_CHANNEL/FINANCE_CHANNEL are separate: company_ops/human_interface.py
# posts to those directly via the Discord REST API, independent of the
# gateway entirely.
for key in DISCORD_BOT_TOKEN DISCORD_ALLOWED_USERS DISCORD_ALLOWED_CHANNELS \
           DISCORD_ALLOW_ALL_USERS DISCORD_HOME_CHANNEL \
           DISCORD_HIL_CHANNEL DISCORD_FINANCE_CHANNEL \
           NOUS_API_KEY OPENCODE_GO_API_KEY ZAI_CODING_PLAN_API_KEY TOKENROUTER_API_KEY \
           STEWARD_MCP_URL STEWARD_TOKEN STEWARD_URL \
           POSTGRES_PASSWORD COMPANY_PASSWORD OBSERVER_PASSWORD ANALYTICS_PASSWORD \
           COMPANY_DATABASE_URL OBSERVER_DATABASE_URL ANALYTICS_DATABASE_URL; do
  val="${!key:-}"
  if [[ -n "$val" ]]; then
    echo "${key}=${val}" >> /opt/data/.env
  fi
done

chmod 644 /opt/data/.env

REPO_DIR="/opt/hermees"
REMOTE="git@github.com:BuildMy-house/hermees-diary.git"

# Clone or pull on start
if [[ -d "$REPO_DIR/.git" ]]; then
  cd "$REPO_DIR" && git pull --ff-only 2>/dev/null || true
else
  git clone "$REMOTE" "$REPO_DIR" 2>/dev/null || true
fi

# Background sync: pull every 30min, commit+push every 2h
sync_loop() {
  while true; do
    sleep 1800  # 30 min
    cd "$REPO_DIR" 2>/dev/null || continue
    git pull --ff-only 2>/dev/null || true
    # Every 4th cycle (~2h), commit and push any local changes
    if [[ $((RANDOM % 4)) -eq 0 ]]; then
      changes=$(git status --porcelain 2>/dev/null | wc -l)
      if [[ "$changes" -gt 0 ]]; then
        git add -A 2>/dev/null || true
        git commit -m "auto-sync: $(date -u +%Y-%m-%dT%H:%M:%SZ)" 2>/dev/null || true
        git push 2>/dev/null || true
      fi
    fi
  done
}
sync_loop &

if [[ "${1:-}" == "hermes" ]]; then
  shift
  exec hermes "$@"
fi

exec company-ops "$@"

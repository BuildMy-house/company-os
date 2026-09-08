#!/usr/bin/env bash
set -euo pipefail

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

chmod -R a+rwx /opt/data 2>/dev/null || true

# Sync config.yaml + SOUL.md from package into mounted volume (first-volume-only fix)
if [ -f /opt/company-ops/hermes/config.yaml ]; then
  cp /opt/company-ops/hermes/config.yaml /opt/data/config.yaml
fi
if [ -f /opt/company-ops/hermes/SOUL.md ]; then
  mkdir -p /root/.hermes
  cp /opt/company-ops/hermes/SOUL.md /root/.hermes/SOUL.md
fi

# Write secrets from environment to /opt/data/.env (hermes reads this at startup)
cat > /opt/data/.env << 'ENVEOF'
# Auto-generated from container environment — do not edit manually
ENVEOF

for key in DISCORD_BOT_TOKEN DISCORD_ALLOWED_USERS DISCORD_ALLOWED_CHANNELS \
           DISCORD_ALLOW_ALL_USERS DISCORD_WORKER_CONFIG DISCORD_WORKER \
           DISCORD_TASK_TYPE DISCORD_TIMEOUT DISCORD_ANNOUNCE_CHANNEL \
           DISCORD_HIL_CHANNEL DISCORD_DM_USER DISCORD_DAILY_PROMPT DISCORD_INVESTOR_PROMPT \
           DISCORD_QUESTIONS_PROMPT DISCORD_INTERVAL_DAILY DISCORD_INTERVAL_INVESTOR \
           NOUS_API_KEY TOKENROUTER_API_KEY OPENCODE_ZEN_API_KEY \
           POSTGRES_PASSWORD COMPANY_PASSWORD OBSERVER_PASSWORD ANALYTICS_PASSWORD \
           COMPANY_DATABASE_URL OBSERVER_DATABASE_URL ANALYTICS_DATABASE_URL; do
  val="${!key:-}"
  if [[ -n "$val" ]]; then
    echo "${key}=${val}" >> /opt/data/.env
  fi
done

chmod 644 /opt/data/.env

REPO_DIR="/opt/hermees"
REMOTE="git@github.com:BuildMy-house/hermees.git"

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

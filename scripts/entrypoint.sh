#!/usr/bin/env bash
set -euo pipefail

chmod -R a+rwx /opt/data 2>/dev/null || true

# Write secrets from environment to /opt/data/.env (hermes reads this at startup)
cat > /opt/data/.env << 'ENVEOF'
# Auto-generated from container environment — do not edit manually
ENVEOF

for key in DISCORD_BOT_TOKEN DISCORD_ALLOWED_USERS DISCORD_ALLOWED_CHANNELS \
           DISCORD_ALLOW_ALL_USERS DISCORD_WORKER_CONFIG DISCORD_WORKER \
           DISCORD_TASK_TYPE DISCORD_TIMEOUT DISCORD_ANNOUNCE_CHANNEL \
           DISCORD_DM_USER DISCORD_DAILY_PROMPT DISCORD_INVESTOR_PROMPT \
           DISCORD_QUESTIONS_PROMPT DISCORD_INTERVAL_DAILY DISCORD_INTERVAL_INVESTOR \
           NOUS_API_KEY TOKENROUTER_API_KEY OPENCODE_ZEN_API_KEY; do
  val="${!key:-}"
  if [[ -n "$val" ]]; then
    echo "${key}=${val}" >> /opt/data/.env
  fi
done

chmod 644 /opt/data/.env

REPO_DIR="/opt/homely-ceo"
REMOTE="git@github.com:NaharEmet/homely-ceo.git"

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

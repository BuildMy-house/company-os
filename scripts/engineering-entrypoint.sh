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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

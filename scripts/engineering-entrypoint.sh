#!/usr/bin/env bash
set -e

mkdir -p /root/.ssh
ssh-keyscan github.com >> /root/.ssh/known_hosts

: "${APP_REPO_URL:=git@github.com:BuildMy-house/app.git}"
: "${WEBSITE_REPO_URL:=git@github.com:BuildMy-house/website.git}"
: "${COMPANY_OS_REPO_URL:=git@github.com:BuildMy-house/company-os.git}"
: "${HERMEES_REPO_URL:=git@github.com:BuildMy-house/hermees.git}"
: "${OBSERVER_WEBSITE_REPO_URL:=git@github.com:BuildMy-house/observer-website.git}"

sync_repo() {
  local name="$1" url="$2" path="$3"
  local attempt max_attempts=3 delay=2 timeout_secs=30

  export GIT_SSH_COMMAND="ssh -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3"

  for (( attempt = 1; attempt <= max_attempts; attempt++ )); do
    if [[ ! -d "$path/.git" ]]; then
      rm -rf "$path" 2>/dev/null
      if timeout "${timeout_secs}s" git clone "$url" "$path" 2>&1; then
        return 0
      fi
    elif timeout "${timeout_secs}s" git -C "$path" fetch origin 2>&1; then
      git -C "$path" reset --hard origin/HEAD 2>/dev/null || true
      return 0
    fi
    echo "WARN: $name sync failed (attempt $attempt/$max_attempts)" >&2
    (( attempt < max_attempts )) && sleep "$delay"
  done
  echo "ERROR: $name failed after $max_attempts attempts — $url" >&2
  return 1
}

for repo in \
  "app:APP_REPO_URL:/workspace/app-checkout" \
  "website:WEBSITE_REPO_URL:/workspace/website-checkout" \
  "company-os:COMPANY_OS_REPO_URL:/workspace/company-os-checkout" \
  "hermees:HERMEES_REPO_URL:/workspace/hermees-checkout" \
  "observer-website:OBSERVER_WEBSITE_REPO_URL:/workspace/observer-website-checkout"; do
  IFS=: read -r name url_var path <<< "$repo"
  sync_repo "$name" "${!url_var}" "$path" || true
done

exec npx -y mcp-proxy --port 8000 -- npx -y ai-cli-mcp@latest

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
  if [[ ! -d "$path/.git" ]]; then
    git clone "$url" "$path" || {
      echo "WARN: failed to sync $name, continuing" >&2
      return 1
    }
  elif ! git -C "$path" fetch origin ||
       ! git -C "$path" reset --hard origin/HEAD; then
    echo "WARN: failed to sync $name, continuing" >&2
    return 1
  fi
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

exec npx -y supergateway --stdio "npx -y ai-cli-mcp@latest" --port 8000 --outputTransport sse

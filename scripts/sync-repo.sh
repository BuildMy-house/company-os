#!/usr/bin/env bash
set -e

# ── On-demand repo sync ──────────────────────────────────────────────────
# Repos are NOT pre-cloned at container startup (that used to sync all 5
# eagerly, which slowed every boot and meant every task paid for repos it
# never touched). Call this before working in a repo you haven't synced
# yet this session — it mints a fresh GitHub App installation token (short
# TTL, so always minted per-call rather than reused from startup) and
# clones (first time) or fetches + hard-resets (subsequent times) the repo.
#
# Usage: sync-repo.sh <app|website|company-os|hermees|observer-website>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

name="${1:?Usage: sync-repo.sh <app|website|company-os|hermees|observer-website>}"

case "$name" in
  app) url_var=APP_REPO_URL; path=/workspace/app-checkout ;;
  website) url_var=WEBSITE_REPO_URL; path=/workspace/website-checkout ;;
  company-os) url_var=COMPANY_OS_REPO_URL; path=/workspace/company-os-checkout ;;
  hermees) url_var=HERMEES_REPO_URL; path=/workspace/hermees-checkout ;;
  observer-website) url_var=OBSERVER_WEBSITE_REPO_URL; path=/workspace/observer-website-checkout ;;
  *)
    echo "ERROR: unknown repo '$name' (expected app|website|company-os|hermees|observer-website)" >&2
    exit 1
    ;;
esac
raw_url="${!url_var:-}"

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
if [[ -n "$GITHUB_TOKEN" ]]; then
  url="https://x-access-token:${GITHUB_TOKEN}@github.com/BuildMy-house/${name}.git"
elif [[ -n "$raw_url" && "$raw_url" != git@* ]]; then
  url="$raw_url"
else
  url="https://github.com/BuildMy-house/${name}.git"
fi
masked_url=$(echo "$url" | mask_tokens)

attempt=0
max_attempts=3
delay=2
timeout_secs=30
while (( attempt < max_attempts )); do
  attempt=$((attempt + 1))
  out="" status=0
  if [[ ! -d "$path/.git" ]]; then
    # Clear contents but leave the directory entry itself alone — it may be
    # a Docker volume mount point, and `rm -rf` on the mount point fails
    # with "Device or resource busy" (which, under `set -e`, would abort
    # the whole script before git clone ever runs). git clone is fine
    # targeting an existing-but-empty directory.
    mkdir -p "$path"
    find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true
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
    exit 0
  fi

  echo "WARN: $name sync failed (attempt $attempt/$max_attempts)" >&2
  (( attempt < max_attempts )) && sleep "$delay"
done

echo "ERROR: $name failed after $max_attempts attempts — $masked_url" >&2
exit 1

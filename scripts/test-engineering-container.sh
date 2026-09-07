#!/usr/bin/env bash
# Self-test script for the engineering container.
# Builds a fresh image and runs a checklist verifying the container is
# fundamentally functional. Repo-clone failures for known/tracked access
# gaps (e.g. company-os without org access) are WARN, not FAIL.
#
# Image is tagged engineering:selftest (never clobbers :candidate/:latest).
# The image is kept after the run for inspection; remove with:
#   docker rmi engineering:selftest
set -uo pipefail

# ── output helpers ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
RESULTS=()
pass() { echo -e "${GREEN}PASS${NC}: $1"; RESULTS+=("PASS: $1"); }
warn() { echo -e "${YELLOW}WARN${NC}: $1"; RESULTS+=("WARN: $1"); }
fail() { echo -e "${RED}FAIL${NC}: $1"; RESULTS+=("FAIL: $1"); }

# ── paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OPS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$OPS_DIR/.." && pwd)"

IMAGE_TAG="engineering:selftest"
CONTAINER_NAME="engineering-selftest-$$"
SSH_KEY="$REPO_ROOT/certs/homely-deploy"

# ── cleanup ──────────────────────────────────────────────────────────
cleanup() {
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# ── determine if SSH key is available for repo cloning ───────────────
# All 5 repos in engineering-entrypoint.sh use SSH URLs (git@github.com:...).
# Even "public" repos need SSH auth for git@ URLs on GitHub.
SSH_MOUNT_ARGS=()
if [[ -f "$SSH_KEY" ]]; then
  SSH_MOUNT_ARGS=(-v "$SSH_KEY:/root/.ssh/id_rsa:ro")
else
  echo "WARN: SSH key not found at $SSH_KEY — SSH-dependent repo clones will fail."
  echo "      This is expected if the deploy key is not provisioned on this host."
fi

echo "══════════════════════════════════════════════════════════════"
echo "Engineering container self-test"
echo "══════════════════════════════════════════════════════════════"

# ── 1. BUILD ─────────────────────────────────────────────────────────
echo ""
echo "── Step 1: Build image ──"
if docker buildx build \
  -f "$OPS_DIR/Dockerfile.engineering" \
  --build-context manager-def="$REPO_ROOT/.claude/agents" \
  --build-context skills-src="$REPO_ROOT/.agents/skills" \
  -t "$IMAGE_TAG" \
  "$OPS_DIR" 2>&1; then
  pass "Image built successfully as $IMAGE_TAG"
else
  fail "Image build failed"
  echo ""
  echo "══════════════════════════════════════════════════════════════"
  echo "RESULTS"
  echo "══════════════════════════════════════════════════════════════"
  for r in "${RESULTS[@]}"; do
    case "$r" in
      FAIL*) echo -e "${RED}$r${NC}" ;;
      WARN*) echo -e "${YELLOW}$r${NC}" ;;
      *)     echo -e "${GREEN}$r${NC}" ;;
    esac
  done
  exit 1
fi

# ── 2. BASIC TOOLS (git, ssh-keyscan) ───────────────────────────────
echo ""
echo "── Step 2: Basic tools resolve ──"
if docker run --rm --entrypoint bash "$IMAGE_TAG" -c \
  'git --version && ssh-keyscan github.com >/dev/null 2>&1 && echo OK'; then
  pass "git and ssh-keyscan resolve"
else
  fail "git or ssh-keyscan missing/broken"
fi

# ── 3. REPO SYNC (via entrypoint) ──────────────────────────────────
echo ""
echo "── Step 3: Repo sync (entrypoint) ──"
# Start a container with the real entrypoint and let it run through repo
# cloning. The entrypoint exec's supergateway, so the container stays up.
# We capture logs to inspect clone outcomes.

# Remove any leftover container with the same name.
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

docker run -d --name "$CONTAINER_NAME" \
  "${SSH_MOUNT_ARGS[@]}" \
  "$IMAGE_TAG" >/dev/null 2>&1

# Wait for the entrypoint to finish its repo sync and start supergateway.
# Give it up to 90 seconds (cloning can be slow on first run).
echo "  Waiting for entrypoint to complete repo sync..."
ENTRIES=0
for i in $(seq 1 90); do
  if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "supergateway\|SSE server started\|Listening on"; then
    ENTRIES=1
    break
  fi
  # Check if container exited early (entrypoint crashed)
  if ! docker inspect --format='{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
    ENTRIES=2
    break
  fi
  sleep 1
done

LOGS=$(docker logs "$CONTAINER_NAME" 2>&1)

if [[ "$ENTRIES" -eq 2 ]]; then
  fail "Entrypoint crashed — container exited before reaching supergateway"
  echo "  Last 30 lines of container logs:"
  echo "$LOGS" | tail -30
elif [[ "$ENTRIES" -eq 0 ]]; then
  fail "Entrypoint did not reach supergateway within 90s"
  echo "  Last 30 lines of container logs:"
  echo "$LOGS" | tail -30
else
  pass "Entrypoint completed and supergateway started"
fi

# Inspect per-repo clone outcomes from the logs.
# The entrypoint prints "WARN: failed to sync <name>, continuing" for failures.
# We also check for unexpected failures (e.g. app/website failing).
REPOS_OK=()
REPOS_WARN=()
REPOS_FAIL=()

for repo_name in app website company-os hermees observer-website; do
  if echo "$LOGS" | grep -q "WARN: failed to sync $repo_name"; then
    # Determine the specific git error to distinguish expected vs unexpected failures
    # git clone errors appear BEFORE the entrypoint's WARN line
    REPO_ERROR=$(echo "$LOGS" | grep -B5 "failed to sync $repo_name" | head -10)
    if echo "$REPO_ERROR" | grep -qi "Repository not found\|Permission denied\|publickey\|authentication"; then
      # Known/tracked access issue — company-os and possibly others
      REPOS_WARN+=("$repo_name")
    else
      # Unexpected failure — app/website should succeed
      REPOS_FAIL+=("$repo_name")
    fi
  else
    REPOS_OK+=("$repo_name")
  fi
done

if [[ ${#REPOS_FAIL[@]} -gt 0 ]]; then
  fail "Unexpected repo clone failures: ${REPOS_FAIL[*]}"
  echo "  These repos should have cloned successfully but didn't."
elif [[ ${#REPOS_WARN[@]} -gt 0 ]]; then
  warn "Repo clone skipped (expected access gap): ${REPOS_WARN[*]}"
  echo "  These repos require SSH deploy key access not yet provisioned (NAHAR-TODO)."
fi

if [[ ${#REPOS_OK[@]} -gt 0 ]]; then
  pass "Repos cloned successfully: ${REPOS_OK[*]}"
fi

# ── 4. CLI RESOLUTION ──────────────────────────────────────────────
echo ""
echo "── Step 4: CLI tools resolve ──"
CLI_CMDS=(
  "claude --version"
  "opencode --version"
  "codex --version"
)
CLI_NAMES=(claude opencode codex)

for i in "${!CLI_CMDS[@]}"; do
  cmd="${CLI_CMDS[$i]}"
  name="${CLI_NAMES[$i]}"
  # Some CLIs don't support --version; fall back to --help
  if docker run --rm --entrypoint bash "$IMAGE_TAG" -c "$cmd" 2>&1 | head -1; then
    pass "$name resolves"
  elif docker run --rm --entrypoint bash "$IMAGE_TAG" -c "${cmd/--version/--help}" 2>&1 | head -1; then
    pass "$name resolves (--help fallback)"
  else
    fail "$name does not resolve"
  fi
done

# ── 5. SUPERGATEWAY STAYING UP ─────────────────────────────────────
echo ""
echo "── Step 5: Supergateway process ──"
# The container from step 3 should still be running with supergateway.
if docker inspect --format='{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
  # pgrep may not be installed in bookworm-slim; scan /proc instead
  if docker exec "$CONTAINER_NAME" bash -c \
    'for f in /proc/*/cmdline; do cat "$f" 2>/dev/null | tr "\0" " " | grep -q supergateway && exit 0; done; exit 1' \
    >/dev/null 2>&1; then
    pass "Supergateway is running"
  else
    fail "Container running but supergateway process not found"
  fi
else
  fail "Container is not running — supergateway may have crashed"
fi

# ── 6. MCP REGISTRATION ───────────────────────────────────────────
echo ""
echo "── Step 6: MCP registration ──"
for cli in claude codex; do
  MCP_LIST=$(docker exec "$CONTAINER_NAME" "$cli" mcp list 2>&1 || true)
  if echo "$MCP_LIST" | grep -q "ai-cli-mcp"; then
    pass "$cli mcp list shows ai-cli-mcp"
  else
    fail "$cli mcp list does NOT show ai-cli-mcp"
    echo "  Output: $MCP_LIST"
  fi
done

# ── SUMMARY ──────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════════"
echo "RESULTS"
echo "══════════════════════════════════════════════════════════════"
HAS_FAIL=0
for r in "${RESULTS[@]}"; do
  case "$r" in
    FAIL*) echo -e "${RED}$r${NC}"; HAS_FAIL=1 ;;
    WARN*) echo -e "${YELLOW}$r${NC}" ;;
    *)     echo -e "${GREEN}$r${NC}" ;;
  esac
done
echo "══════════════════════════════════════════════════════════════"

if [[ "$HAS_FAIL" -eq 1 ]]; then
  echo ""
  echo "Self-test completed with FAILURES — the container has real problems."
  exit 1
else
  echo ""
  echo "Self-test completed (no FAIL results). Image $IMAGE_TAG retained for inspection."
  exit 0
fi

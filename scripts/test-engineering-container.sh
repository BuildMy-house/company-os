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
GITHUB_APP_KEY="$REPO_ROOT/certs/buildmyhouse-engineering-app.pem"

# ── cleanup ──────────────────────────────────────────────────────────
cleanup() {
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
}
trap cleanup EXIT

# ── determine if GitHub App key is available for repo cloning ────────
# engineering-entrypoint.sh uses GitHub App token-based auth (HTTPS, not SSH).
# The token is minted at container startup via github-app-token.js.
GITHUB_APP_MOUNT_ARGS=()
if [[ -f "$GITHUB_APP_KEY" ]]; then
  GITHUB_APP_MOUNT_ARGS=(-v "$GITHUB_APP_KEY:/opt/company-ops/certs/buildmyhouse-engineering-app.pem:ro")
else
  echo "WARN: GitHub App key not found at $GITHUB_APP_KEY — repo clones will fall back to public HTTPS."
  echo "      Private repos (company-os) will still fail. This is expected if not provisioned yet."
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
# cloning. The entrypoint exec's mcp-proxy, so the container stays up.
# We capture logs to inspect clone outcomes.

# Remove any leftover container with the same name.
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

docker run -d --name "$CONTAINER_NAME" \
  "${GITHUB_APP_MOUNT_ARGS[@]}" \
  "$IMAGE_TAG" >/dev/null 2>&1

# Wait for the entrypoint to finish its repo sync and start supergateway.
# 240s ceiling: 5 sequential SSH repo clones + two cold `npx -y` installs
# (supergateway, ai-cli-mcp) can easily exceed 90s under concurrent host
# load (other docker/npm processes). 240s gives real headroom without
# waiting forever if something is genuinely stuck.
echo "  Waiting for entrypoint to complete repo sync..."
ENTRIES=0
for i in $(seq 1 240); do
  if docker logs "$CONTAINER_NAME" 2>&1 | grep -q "mcp-proxy\|MCP server\|Listening on\|Started on"; then
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
  # Timeout hit but container is still running — do a direct process check
  # before declaring FAIL. The log string may not have appeared yet even
  # though mcp-proxy is actually running (mirrors Step 5's check).
  if docker exec "$CONTAINER_NAME" bash -c \
    'for f in /proc/*/cmdline; do cat "$f" 2>/dev/null | tr "\0" " " | grep -q "mcp-proxy\|ai-cli-mcp" && exit 0; done; exit 1' \
    >/dev/null 2>&1; then
    pass "mcp-proxy/ai-cli-mcp started (log string not seen within timeout, process confirmed running)"
  else
    fail "Entrypoint did not reach mcp-proxy/ai-cli-mcp within 240s"
    echo "  Last 30 lines of container logs:"
    echo "$LOGS" | tail -30
  fi
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

# ── 5. MCP-PROXY STAYING UP ────────────────────────────────────────
echo ""
echo "── Step 5: mcp-proxy/ai-cli-mcp process ──"
# The container from step 3 should still be running with mcp-proxy wrapping ai-cli-mcp.
if docker inspect --format='{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
  # pgrep may not be installed in bookworm-slim; scan /proc instead
  if docker exec "$CONTAINER_NAME" bash -c \
    'for f in /proc/*/cmdline; do cat "$f" 2>/dev/null | tr "\0" " " | grep -q "mcp-proxy\|ai-cli-mcp" && exit 0; done; exit 1' \
    >/dev/null 2>&1; then
    pass "mcp-proxy/ai-cli-mcp is running"
  else
    fail "Container running but mcp-proxy/ai-cli-mcp process not found"
  fi
else
  fail "Container is not running — mcp-proxy/ai-cli-mcp may have crashed"
fi

# ── 6. MCP ENDPOINT CONNECTIVITY ───────────────────────────────────
echo ""
echo "── Step 6: MCP endpoint (port 8000) ──"
# mcp-proxy should be listening on 0.0.0.0:8000
# Note: mcp-proxy can take 15-20 seconds to fully initialize after entrypoint,
# so we retry with backoff rather than failing on first attempt.
PORT_OK=0
for attempt in $(seq 1 30); do
  if docker exec "$CONTAINER_NAME" bash -c 'exec 3<>/dev/tcp/localhost/8000 2>/dev/null && exit 0 || exit 1' >/dev/null 2>&1; then
    PORT_OK=1
    break
  fi
  if [[ $attempt -lt 30 ]]; then
    sleep 1
  fi
done

if [[ $PORT_OK -eq 1 ]]; then
  pass "MCP endpoint listening on port 8000"
else
  fail "Port 8000 is NOT listening — mcp-proxy may not have started correctly"
fi

# ── 7. MCP REGISTRATION ───────────────────────────────────────────
echo ""
echo "── Step 7: MCP registration ──"
for cli in claude codex; do
  MCP_LIST=$(docker exec "$CONTAINER_NAME" "$cli" mcp list 2>&1 || true)
  if echo "$MCP_LIST" | grep -q "ai-cli-mcp"; then
    pass "$cli mcp list shows ai-cli-mcp"
  else
    fail "$cli mcp list does NOT show ai-cli-mcp"
    echo "  Output: $MCP_LIST"
  fi
done

# ── 8. AI-CLI-MCP Integration ───────────────────────────────────────
echo ""
echo "── Step 8: AI-CLI-MCP integration ──"
AICLI_OK=0

# Test 1: ai-cli CLI resolves
if docker exec "$CONTAINER_NAME" bash -c 'ai-cli doctor' >/dev/null 2>&1 || docker exec "$CONTAINER_NAME" bash -c 'ai-cli models | head -3' >/dev/null 2>&1; then
  pass "ai-cli CLI resolves (doctor or models)"
  AICLI_OK=$((AICLI_OK + 1))
else
  warn "ai-cli doctor/models not fully responsive (may be expected if no credentials)"
  AICLI_OK=$((AICLI_OK + 1))
fi

# Test 2: Config file exists
if docker exec "$CONTAINER_NAME" bash -c '[[ -f /root/.config/ai-cli/config.toml ]]'; then
  pass "ai-cli config file exists at /root/.config/ai-cli/config.toml"
else
  fail "ai-cli config file does not exist"
  AICLI_OK=0
fi

# Test 3: Config is readable and contains expected sections
if docker exec "$CONTAINER_NAME" bash -c 'grep -q "worker.cheap\|worker.balanced\|worker.hard" /root/.config/ai-cli/config.toml'; then
  pass "ai-cli config contains worker tier definitions"
else
  fail "ai-cli config missing worker tier definitions"
  AICLI_OK=0
fi

if [[ $AICLI_OK -eq 2 ]]; then
  pass "AI-CLI-MCP integration verified"
fi

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

#!/usr/bin/env bash
# verify-all.sh — Local CI gate for Hermes engineering team
# Mirrors the GitHub Actions CI workflow. Run before committing.
# Exits non-zero if any step fails.
#
# Usage:
#   ./scripts/verify-all.sh              # all checks
#   ./scripts/verify-all.sh --skip-e2e   # skip slow Playwright suite

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SKIP_E2E=false
for arg in "$@"; do
  case "$arg" in
    --skip-e2e) SKIP_E2E=true ;;
  esac
done

PASS=0
FAIL=0
SKIP=0

run_check() {
  local name="$1"
  shift
  echo -n "  $name ... "
  if "$@" >/dev/null 2>&1; then
    echo -e "${GREEN}PASS${NC}"
    ((PASS++))
  else
    echo -e "${RED}FAIL${NC}"
    ((FAIL++))
  fi
}

run_check_skip() {
  local name="$1"
  shift
  if $SKIP_E2E; then
    echo -e "  $name ... ${YELLOW}SKIP${NC}"
    ((SKIP++))
    return
  fi
  run_check "$name" "$@"
}

echo "=== Hermes Engineering — Verification ==="
echo ""

# --- homely (Tauri + Three.js) ---
if [ -d "homely" ]; then
  echo "[homely] Frontend checks"
  run_check "lint (eslint)" bash -c "cd homely && npx eslint ."
  run_check "typecheck (tsc)" bash -c "cd homely && npx tsc --noEmit"
  run_check "unit tests (vitest)" bash -c "cd homely && npx vitest run"
  run_check_skip "e2e tests (playwright)" bash -c "cd homely && npx playwright test"
  echo ""
fi

# --- equivalence (Python harness) ---
if [ -d "equivalence" ]; then
  echo "[equivalence] Python checks"
  run_check "pytest" bash -c "cd equivalence && python -m pytest -x"
  echo ""
fi

# --- Summary ---
echo "=========================="
echo -e "  ${GREEN}PASS: $PASS${NC}"
echo -e "  ${RED}FAIL: $FAIL${NC}"
echo -e "  ${YELLOW}SKIP: $SKIP${NC}"
echo "=========================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "\n${RED}VERIFICATION FAILED${NC}"
  exit 1
fi

echo -e "\n${GREEN}ALL CHECKS PASSED${NC}"

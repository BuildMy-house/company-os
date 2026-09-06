#!/usr/bin/env bash
# staging-promote.sh — record a staging deployment in the company ledger.
#
# Usage: ./scripts/staging-promote.sh <commit> [--dry-run]
#
# What it does:
#   1. Starts test Postgres, applies schema (via test-db-up.sh)
#   2. Creates a venv + installs company-ops (if needed)
#   3. Runs pytest against the test DB
#   4. If all tests pass, records the deployment via `company-ops deployment record`
#
# If any step fails, exits non-zero and does NOT record the deployment.
# --dry-run: skip the actual deployment record, just run tests.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV="$REPO_ROOT/.venv"
COMPANY_OPS="$REPO_ROOT/company-ops"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
  esac
done

COMMIT="${1:?Usage: $0 <commit> [--dry-run]}"

# ── 1. Ensure test DB is up ────────────────────────────────────────────
echo "==> Starting test Postgres..."
"$COMPANY_OPS/scripts/test-db-up.sh"

# ── 2. Ensure company-ops is installed in the venv ────────────────────
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
  echo "ERROR: No venv at $VENV — create one first: python3 -m venv $VENV" >&2
  exit 1
fi
if ! "$PY" -c "import company_ops" 2>/dev/null; then
  echo "==> Installing company-ops in venv..."
  "$PY" -m pip install -e "$COMPANY_OPS[test]" -q
fi

# ── 3. Run pytest ──────────────────────────────────────────────────────
echo "==> Running company-ops tests..."
export TEST_COMPANY_DATABASE_URL="postgresql://hermes_company:localtest_company@localhost:5544/homely_company"
"$PY" -m pytest "$COMPANY_OPS/tests" -v

# ── 4. Record the deployment (unless --dry-run) ───────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> [dry-run] Would record deployment: staging $COMMIT"
  exit 0
fi

echo "==> Recording deployment: staging $COMMIT"
DEPLOY_ID=$("$PY" -m company_ops deployment record staging "$COMMIT")
echo "$DEPLOY_ID"

echo ""
echo "=== Deployment recorded ==="
echo "  Commit:    $COMMIT"
echo "  Environ:   staging"
echo "  Ledger:    $DEPLOY_ID"

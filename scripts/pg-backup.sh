#!/usr/bin/env bash
set -euo pipefail

# Thin cron-friendly wrapper: real logic lives in company_ops/backup.py.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"
exec python -m company_ops.backup

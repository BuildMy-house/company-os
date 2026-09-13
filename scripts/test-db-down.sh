#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"

echo "==> Stopping test Postgres and removing volume..."
docker compose -f company-ops/docker-compose.test.yml down -v

echo "==> Test Postgres stopped and volume removed."

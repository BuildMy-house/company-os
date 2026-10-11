#!/usr/bin/env bash
set -euo pipefail

# Resolve to repo root regardless of where script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

echo "==> Starting test Postgres via docker compose..."
docker compose -f docker-compose.test.yml up -d

echo "==> Waiting for Postgres to be ready..."
MAX_RETRIES=30
for i in $(seq 1 "$MAX_RETRIES"); do
    if docker exec company-ops-test-postgres pg_isready -U postgres >/dev/null 2>&1; then
        echo "    Postgres ready after ${i}s"
        break
    fi
    if [ "$i" -eq "$MAX_RETRIES" ]; then
        echo "ERROR: Postgres did not become ready after ${MAX_RETRIES}s" >&2
        exit 1
    fi
    sleep 1
done

# Apply schema files if they exist.
for sql_file in sql/company_schema.sql sql/observer_schema.sql sql/pm_schema.sql; do
    if [ -f "$sql_file" ]; then
        echo "==> Applying $sql_file ..."
        docker exec -i company-ops-test-postgres psql -U postgres -d homely_company -f - < "$sql_file"
    else
        echo "WARN: $sql_file not found — skipping (parallel ticket may not have landed yet)" >&2
    fi
done

# Provision roles from sql/roles.d with local dev passwords (scratch DB only).
echo "==> Provisioning roles via scripts/provision-db-roles.sh ..."
PGHOST=localhost PGPORT=5544 PGPASSWORD=localtestpw \
COMPANY_PASSWORD=localtest_company OBSERVER_PASSWORD=localtest_observer \
ANALYTICS_PASSWORD=localtest_analytics PM_AGENT_WRITER_PASSWORD=localtest_pm_agent \
    "$REPO_ROOT/scripts/provision-db-roles.sh"

echo ""
echo "=== Test Postgres is running ==="
echo "  Container: company-ops-test-postgres"
echo "  Host port: 5544 -> container 5432"
echo "  Database:  homely_company"
echo ""
echo "Connection strings:"
echo "  TEST_COMPANY_DATABASE_URL=postgresql://hermes_company:localtest_company@localhost:5544/homely_company"
echo "  TEST_OBSERVER_DATABASE_URL=postgresql://hermes_observer_writer:localtest_observer@localhost:5544/homely_company"
echo "  TEST_ANALYTICS_DATABASE_URL=postgresql://hermes_analytics:localtest_analytics@localhost:5544/homely_company"

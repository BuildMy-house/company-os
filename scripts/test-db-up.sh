#!/usr/bin/env bash
set -euo pipefail

# Resolve to repo root regardless of where script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"

echo "==> Starting test Postgres via docker compose..."
docker compose -f company-ops/docker-compose.test.yml up -d

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
for sql_file in company-ops/sql/company_schema.sql company-ops/sql/observer_schema.sql; do
    if [ -f "$sql_file" ]; then
        echo "==> Applying $sql_file ..."
        docker exec -i company-ops-test-postgres psql -U postgres -d homely_company -f - < "$sql_file"
    else
        echo "WARN: $sql_file not found — skipping (parallel ticket may not have landed yet)" >&2
    fi
done

# Apply roles.sql and set local dev passwords.
if [ -f company-ops/sql/roles.sql ]; then
    echo "==> Applying company-ops/sql/roles.sql ..."
    docker exec -i company-ops-test-postgres psql -U postgres -d homely_company -f - < company-ops/sql/roles.sql

    echo "==> Setting local dev passwords for roles..."
    docker exec company-ops-test-postgres psql -U postgres -d homely_company -c \
        "ALTER ROLE hermes_company WITH PASSWORD 'localtest_company';"
    docker exec company-ops-test-postgres psql -U postgres -d homely_company -c \
        "ALTER ROLE hermes_observer_writer WITH PASSWORD 'localtest_observer';"
    docker exec company-ops-test-postgres psql -U postgres -d homely_company -c \
        "ALTER ROLE hermes_analytics WITH PASSWORD 'localtest_analytics';"
else
    echo "WARN: company-ops/sql/roles.sql not found — skipping role setup (parallel ticket may not have landed yet)" >&2
fi

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

#!/usr/bin/env bash
set -euo pipefail

# init-postgres.sh — First-run schema setup for production Postgres.
# Runs automatically via docker-entrypoint-initdb.d on first container start.
# Applies schemas only; roles are provisioned by scripts/provision-db-roles.sh.

echo "==> Applying company_schema.sql ..."
psql -U postgres -d homely_company -f /opt/sql/company_schema.sql

echo "==> Applying observer_schema.sql ..."
psql -U postgres -d homely_company -f /opt/sql/observer_schema.sql

echo "==> Schemas applied. Roles, passwords and grants are reconciled by"
echo "    scripts/provision-db-roles.sh (manifests in sql/roles.d/)."

#!/usr/bin/env bash
set -euo pipefail

# init-postgres.sh — First-run schema + role setup for production Postgres.
# Runs automatically via docker-entrypoint-initdb.d on first container start.
# Applies schemas, then roles, then sets real passwords from env vars.

echo "==> Applying company_schema.sql ..."
psql -U postgres -d homely_company -f /opt/sql/company_schema.sql

echo "==> Applying observer_schema.sql ..."
psql -U postgres -d homely_company -f /opt/sql/observer_schema.sql

echo "==> Applying roles.sql ..."
psql -U postgres -d homely_company -f /opt/sql/roles.sql

echo "==> Schema + roles applied. Passwords will be set by 04-set-role-passwords.sh."

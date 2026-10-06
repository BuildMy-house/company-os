#!/usr/bin/env bash
set -euo pipefail

# 04-set-role-passwords.sh — Set real passwords for production Postgres roles.
# Run ONCE after the schema + roles.sql have been applied by init-postgres.sh.
# Reads passwords from environment variables (injected via docker-compose env_file).
# Idempotent: safe to re-run (ALTER ROLE ... WITH PASSWORD is always safe).

if [ -z "${COMPANY_PASSWORD:-}" ] || [ -z "${OBSERVER_PASSWORD:-}" ] || [ -z "${ANALYTICS_PASSWORD:-}" ]; then
  echo "ERROR: COMPANY_PASSWORD, OBSERVER_PASSWORD, ANALYTICS_PASSWORD must be set" >&2
  exit 1
fi

# PM_AGENT_WRITER_PASSWORD is optional here (pm_agent_writer is a newer role,
# PM-C) so this script keeps working for deployments that haven't applied
# sql/roles.sql's pm_agent_writer block yet -- warn instead of hard-failing.
if [ -z "${PM_AGENT_WRITER_PASSWORD:-}" ]; then
  echo "WARNING: PM_AGENT_WRITER_PASSWORD not set -- pm_agent_writer will keep" >&2
  echo "  its sql/roles.sql placeholder password (CHANGE_ME_PM_AGENT_WRITER_PASSWORD)." >&2
fi

PGPASSWORD="${POSTGRES_PASSWORD}" psql -U postgres -d homely_company -c \
  "ALTER ROLE hermes_company WITH PASSWORD '${COMPANY_PASSWORD}';"

PGPASSWORD="${POSTGRES_PASSWORD}" psql -U postgres -d homely_company -c \
  "ALTER ROLE hermes_observer_writer WITH PASSWORD '${OBSERVER_PASSWORD}';"

PGPASSWORD="${POSTGRES_PASSWORD}" psql -U postgres -d homely_company -c \
  "ALTER ROLE hermes_analytics WITH PASSWORD '${ANALYTICS_PASSWORD}';"

if [ -n "${PM_AGENT_WRITER_PASSWORD:-}" ]; then
  PGPASSWORD="${POSTGRES_PASSWORD}" psql -U postgres -d homely_company -c \
    "ALTER ROLE pm_agent_writer WITH PASSWORD '${PM_AGENT_WRITER_PASSWORD}';"
fi

echo "Role passwords updated."

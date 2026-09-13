-- roles.sql: Create Postgres roles for the company/observer schemas.
-- MUST be run once by a superuser or database owner (e.g. Neon's default role,
-- or a local docker Postgres superuser).
-- Run with: psql -f roles.sql <connection-string>
-- Idempotent: safe to run multiple times (uses IF NOT EXISTS guards).

-- =============================================================================
-- 1. Login roles
-- =============================================================================

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hermes_company') THEN
    CREATE ROLE hermes_company LOGIN PASSWORD 'CHANGE_ME_COMPANY_PASSWORD';
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hermes_observer_writer') THEN
    CREATE ROLE hermes_observer_writer LOGIN PASSWORD 'CHANGE_ME_OBSERVER_WRITER_PASSWORD';
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hermes_analytics') THEN
    CREATE ROLE hermes_analytics LOGIN PASSWORD 'CHANGE_ME_ANALYTICS_PASSWORD';
  END IF;
END $$;

-- =============================================================================
-- 2. hermes_company: full CRUD on company schema (current + future tables)
-- =============================================================================

GRANT USAGE ON SCHEMA company TO hermes_company;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA company TO hermes_company;
ALTER DEFAULT PRIVILEGES IN SCHEMA company
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hermes_company;

-- =============================================================================
-- 3. hermes_observer_writer: INSERT + SELECT only on observer schema
--    (no UPDATE, DELETE, or TRUNCATE — append-only by permission)
-- =============================================================================

GRANT USAGE ON SCHEMA observer TO hermes_observer_writer;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA observer TO hermes_observer_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA observer
  GRANT SELECT, INSERT ON TABLES TO hermes_observer_writer;

-- Defense-in-depth: explicitly revoke write operations even though they were
-- never granted. Makes the append-only intent unambiguous to future readers.
REVOKE UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA observer FROM hermes_observer_writer;

-- =============================================================================
-- 4. hermes_analytics: SELECT-only on both schemas (current + future tables)
-- =============================================================================

GRANT USAGE ON SCHEMA company TO hermes_analytics;
GRANT USAGE ON SCHEMA observer TO hermes_analytics;
GRANT SELECT ON ALL TABLES IN SCHEMA company TO hermes_analytics;
GRANT SELECT ON ALL TABLES IN SCHEMA observer TO hermes_analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA company
  GRANT SELECT ON TABLES TO hermes_analytics;
ALTER DEFAULT PRIVILEGES IN SCHEMA observer
  GRANT SELECT ON TABLES TO hermes_analytics;

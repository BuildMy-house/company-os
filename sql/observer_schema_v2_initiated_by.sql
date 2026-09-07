-- observer_schema_v2_initiated_by.sql: Add initiated_by to predictions and experiments.
-- Idempotent: safe to run multiple times (uses IF NOT EXISTS).

ALTER TABLE observer.predictions ADD COLUMN IF NOT EXISTS initiated_by TEXT;
ALTER TABLE observer.experiments ADD COLUMN IF NOT EXISTS initiated_by TEXT;

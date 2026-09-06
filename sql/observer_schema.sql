-- observer_schema.sql: Create the observer schema with append-only audit/evidence tables.
-- Run with: psql -f observer_schema.sql <connection-string>
-- Idempotent: safe to run multiple times.
-- UPDATE/DELETE/TRUNCATE are denied at the role level (see roles.sql).

CREATE SCHEMA IF NOT EXISTS observer;

CREATE TABLE IF NOT EXISTS observer.decisions (
  id TEXT PRIMARY KEY,
  problem TEXT,
  evidence_refs TEXT,
  reasoning_summary TEXT,
  alternatives_considered TEXT,
  decision TEXT,
  confidence TEXT,
  expected_outcome TEXT,
  initiated_by TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.predictions (
  id TEXT PRIMARY KEY,
  decision_id TEXT,
  metric TEXT,
  target_value TEXT,
  confidence TEXT,
  evaluation_date TEXT,
  actual_value TEXT,
  outcome TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.experiments (
  id TEXT PRIMARY KEY,
  name TEXT,
  hypothesis TEXT,
  status TEXT,
  started_at TEXT,
  decided_at TEXT,
  decision TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.human_requests (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  question TEXT NOT NULL,
  reason TEXT,
  importance TEXT,
  blocking BOOLEAN NOT NULL DEFAULT FALSE,
  related_ids TEXT,
  initiated_by TEXT,
  references_id TEXT,
  completed_at TEXT,
  human_minutes NUMERIC,
  outcome TEXT,
  avoidable BOOLEAN,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.human_discoveries (
  id TEXT PRIMARY KEY,
  request_id TEXT,
  discovery TEXT,
  source TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.failures (
  id TEXT PRIMARY KEY,
  description TEXT,
  detected_by TEXT,
  severity TEXT,
  related_ids TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.recoveries (
  id TEXT PRIMARY KEY,
  failure_id TEXT,
  description TEXT,
  recovered_by TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.autonomy_events (
  id TEXT PRIMARY KEY,
  dimension TEXT,
  event_type TEXT,
  initiated_by TEXT,
  related_ids TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.human_minutes (
  id TEXT PRIMARY KEY,
  request_id TEXT,
  minutes NUMERIC,
  activity TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observer.relationships (
  id TEXT PRIMARY KEY,
  from_id TEXT,
  to_id TEXT,
  relation_type TEXT,
  created_at TEXT NOT NULL
);

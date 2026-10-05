-- pm_schema.sql: Create the PM-agent conversation and feedback-signal tables.
-- Run with: psql -f pm_schema.sql <connection-string>
-- Idempotent: safe to run multiple times.

CREATE SCHEMA IF NOT EXISTS company;

CREATE TABLE IF NOT EXISTS company.pm_conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  app_user_id TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_message_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  summary TEXT,
  messages JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS company.feedback_signals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  topic TEXT NOT NULL,
  normalized_topic TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  example_quotes JSONB NOT NULL DEFAULT '[]'::jsonb,
  linked_hive_work_id TEXT,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','escalated','filed','dismissed'))
);

-- Non-unique on purpose: a manual merge may consolidate rows later.
CREATE INDEX IF NOT EXISTS feedback_signals_normalized_topic_idx
  ON company.feedback_signals (normalized_topic);

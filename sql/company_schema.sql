-- company_schema.sql: Create the company schema with all Hermes mutable-data tables.
-- Run with: psql -f company_schema.sql <connection-string>
-- Idempotent: safe to run multiple times.

CREATE SCHEMA IF NOT EXISTS company;

CREATE TABLE IF NOT EXISTS company.plans (
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  owner TEXT NOT NULL,
  status TEXT NOT NULL,
  budget_credits INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  result TEXT
);

CREATE TABLE IF NOT EXISTS company.actions (
  id TEXT PRIMARY KEY,
  plan_id TEXT,
  agent TEXT NOT NULL,
  action_type TEXT NOT NULL,
  credits INTEGER NOT NULL,
  provider_cost_cents INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  result TEXT,
  FOREIGN KEY(plan_id) REFERENCES company.plans(id)
);

CREATE TABLE IF NOT EXISTS company.credit_transactions (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  amount INTEGER NOT NULL,
  reference TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company.provider_usage (
  id TEXT PRIMARY KEY,
  action_id TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  provider_cost_cents INTEGER NOT NULL,
  capacity_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(action_id) REFERENCES company.actions(id)
);

CREATE TABLE IF NOT EXISTS company.revenue_events (
  id TEXT PRIMARY KEY,
  net_revenue_cents INTEGER NOT NULL,
  source TEXT NOT NULL,
  credits_created INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company.tool_requests (
  id TEXT PRIMARY KEY,
  requested_by TEXT NOT NULL,
  name TEXT NOT NULL,
  purpose TEXT NOT NULL,
  cost_credits INTEGER NOT NULL,
  risk TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company.deployments (
  id TEXT PRIMARY KEY,
  environment TEXT NOT NULL,
  version TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  result TEXT
);

CREATE TABLE IF NOT EXISTS company.routing_lessons (
  id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  task_type TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(model, task_type)
);

CREATE TABLE IF NOT EXISTS company.snapshots (
  id TEXT PRIMARY KEY,
  balance INTEGER NOT NULL,
  plans_count INTEGER NOT NULL,
  actions_count INTEGER NOT NULL,
  revenue_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

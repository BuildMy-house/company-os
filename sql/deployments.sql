-- companyd deployment history and runtime control schema
-- Phase 1: Core tracking and generation pointer

CREATE TABLE IF NOT EXISTS deployments (
  id TEXT PRIMARY KEY,
  component TEXT NOT NULL,
  generation TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  docker_image TEXT,
  requested_by TEXT,
  reason TEXT,
  status TEXT NOT NULL,

  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  build_start TIMESTAMP,
  build_end TIMESTAMP,
  test_start TIMESTAMP,
  test_end TIMESTAMP,
  activate_time TIMESTAMP,
  drain_start TIMESTAMP,
  drain_end TIMESTAMP,

  previous_generation TEXT,
  rollback_required BOOLEAN DEFAULT FALSE,
  rollback_reason TEXT,

  tests_passed BOOLEAN,
  test_summary TEXT,
  self_test_passed BOOLEAN,
  health_check_passed BOOLEAN,

  active_tasks_at_drain INTEGER,
  drain_duration_seconds INTEGER,

  human_intervention_required BOOLEAN DEFAULT FALSE,
  notes TEXT,

  UNIQUE(component, generation)
);

CREATE TABLE IF NOT EXISTS runtime_control (
  component TEXT PRIMARY KEY,
  active_generation TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_deployments_component_status
  ON deployments(component, status);

CREATE INDEX IF NOT EXISTS idx_deployments_created_at
  ON deployments(created_at DESC);

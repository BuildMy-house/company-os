-- marketing_schema.sql: aggregate marketing analytics, filled by the
-- deterministic ingester (company_ops/marketing_ingest.py), read by the
-- publisher agent (publisher_reader) and Hermes (hermes_analytics).
-- Aggregate data only: no user-level rows, no PII columns.
-- Run with: psql -f marketing_schema.sql <connection-string>
-- Idempotent: safe to run multiple times.

CREATE SCHEMA IF NOT EXISTS marketing;

-- GA4 Data API: one row per day, page and default channel group.
CREATE TABLE IF NOT EXISTS marketing.ga4_daily (
  date DATE NOT NULL,
  property_id TEXT NOT NULL,
  page_path TEXT NOT NULL,
  channel_group TEXT NOT NULL,
  sessions BIGINT NOT NULL DEFAULT 0,
  total_users BIGINT NOT NULL DEFAULT 0,
  screen_page_views BIGINT NOT NULL DEFAULT 0,
  engaged_sessions BIGINT NOT NULL DEFAULT 0,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (date, property_id, page_path, channel_group)
);

-- Search Console searchanalytics: one row per day, page and query.
CREATE TABLE IF NOT EXISTS marketing.gsc_daily (
  date DATE NOT NULL,
  site_url TEXT NOT NULL,
  page TEXT NOT NULL,
  query TEXT NOT NULL,
  clicks BIGINT NOT NULL DEFAULT 0,
  impressions BIGINT NOT NULL DEFAULT 0,
  ctr DOUBLE PRECISION NOT NULL DEFAULT 0,
  position DOUBLE PRECISION NOT NULL DEFAULT 0,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (date, site_url, page, query)
);

-- Reddit: daily snapshot of score/comments for posts our own account made.
CREATE TABLE IF NOT EXISTS marketing.reddit_posts_daily (
  snapshot_date DATE NOT NULL,
  subreddit TEXT NOT NULL,
  post_id TEXT NOT NULL,
  title TEXT NOT NULL,
  permalink TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  score BIGINT NOT NULL DEFAULT 0,
  num_comments BIGINT NOT NULL DEFAULT 0,
  upvote_ratio DOUBLE PRECISION,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, subreddit, post_id)
);

-- Substack: post inventory from the public RSS feed. Substack has no
-- official stats API, so there are deliberately no view/subscriber columns.
CREATE TABLE IF NOT EXISTS marketing.substack_posts (
  snapshot_date DATE NOT NULL,
  publication TEXT NOT NULL,
  post_url TEXT NOT NULL,
  title TEXT NOT NULL,
  published_at TIMESTAMPTZ,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, publication, post_url)
);

-- One row per source per ingest attempt.
CREATE TABLE IF NOT EXISTS marketing.ingest_runs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('ga4','gsc','reddit','substack')),
  window_start DATE NOT NULL,
  window_end DATE NOT NULL,
  rows_upserted INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('ok','failed','skipped','unavailable')),
  error TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ingest_runs_source_started_idx
  ON marketing.ingest_runs (source, started_at DESC);

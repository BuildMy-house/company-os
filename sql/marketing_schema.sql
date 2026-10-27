-- marketing_schema.sql: aggregate marketing analytics, filled by the
-- deterministic ingester (company_ops/marketing_ingest.py), read by the
-- marketing-lead agent (publisher_reader) and Hermes (hermes_analytics).
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

-- Substack (unofficial, cookie-based substack-mcp; see company_ops/substack_bridge.py).
-- Aggregates only: no subscriber-level rows, no emails, ever.
CREATE TABLE IF NOT EXISTS marketing.substack_publication_daily (
  snapshot_date DATE NOT NULL,
  publication TEXT NOT NULL,
  subscribers BIGINT,
  subscribers_last_30_days BIGINT,
  email_subscribers BIGINT,
  app_subscribers BIGINT,
  views BIGINT,
  views_30d BIGINT,
  open_rate_30d DOUBLE PRECISION,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, publication)
);

CREATE TABLE IF NOT EXISTS marketing.substack_post_stats (
  snapshot_date DATE NOT NULL,
  publication TEXT NOT NULL,
  post_id BIGINT NOT NULL,
  title TEXT NOT NULL,
  post_date TIMESTAMPTZ,
  delivered BIGINT,
  opens BIGINT,
  open_rate DOUBLE PRECISION,
  clicks BIGINT,
  views BIGINT,
  signups BIGINT,
  subscribes BIGINT,
  unsubscribes BIGINT,
  likes BIGINT,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_date, publication, post_id)
);

-- One row per source per ingest attempt.
CREATE TABLE IF NOT EXISTS marketing.ingest_runs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('ga4','gsc','reddit','substack')),
  window_start DATE NOT NULL,
  window_end DATE NOT NULL,
  rows_upserted INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('ok','failed','skipped','unavailable','auth_failed')),
  error TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ingest_runs_source_started_idx
  ON marketing.ingest_runs (source, started_at DESC);

-- Search Console searchanalytics with the full dimension set, filled by
-- company_ops/seo_gsc_ingest.py (service account, 16-month backfill). This is the
-- single Search Console table; the old OAuth-fed marketing.gsc_daily was dropped
-- (sql/marketing_schema_v2_drop_gsc_daily.sql).
CREATE TABLE IF NOT EXISTS marketing.gsc_search_analytics (
  site_url TEXT NOT NULL,
  date DATE NOT NULL,
  query TEXT NOT NULL,
  page TEXT NOT NULL,
  country TEXT NOT NULL,
  device TEXT NOT NULL,
  clicks INTEGER NOT NULL DEFAULT 0,
  impressions INTEGER NOT NULL DEFAULT 0,
  ctr DOUBLE PRECISION NOT NULL DEFAULT 0,
  position DOUBLE PRECISION NOT NULL DEFAULT 0,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (site_url, date, query, page, country, device)
);

CREATE INDEX IF NOT EXISTS gsc_search_analytics_site_date_idx
  ON marketing.gsc_search_analytics (site_url, date);

CREATE INDEX IF NOT EXISTS gsc_search_analytics_site_page_idx
  ON marketing.gsc_search_analytics (site_url, page);

-- marketing_schema_v2_drop_gsc_daily.sql: retire the OAuth-fed Search Console table.
-- marketing.gsc_search_analytics (company_ops/seo_gsc_ingest.py, service account) is the single
-- Search Console source; marketing-ingest no longer fetches GSC and nothing reads gsc_daily.
-- Idempotent. NOT applied by this change: a human runs it after the new marketing-ingest image
-- is live (docs/SEO-GSC-INGEST.md "Rollout"). Destructive: the table's rows are not copied
-- (gsc_search_analytics holds a 16-month backfill of the same data with more dimensions).

DROP TABLE IF EXISTS marketing.gsc_daily;

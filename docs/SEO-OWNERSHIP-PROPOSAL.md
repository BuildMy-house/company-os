# SEO ownership: the marketing-lead owns the website and SEO

Status: decided. The `publisher` agent was renamed `marketing-lead` and owns the website (BuildMy-house/website) and all SEO, blog, Substack and Reddit posting. Hermes stays the approver: publishing to Reddit or Substack still requires `metadata.approval_ref`, and merging any PR stays a human decision.

## Cadence

One post or site improvement every 2-3 days (cron `0 6 */3 * *` for the cycle; ingest stays daily), driven by Search Console data: queries with impressions but no page, and pages with low CTR at a good position. Each cycle the marketing-lead reads the data, picks one change, opens a PR (`seo/<slug>` or `blog/<slug>`), logs the hypothesis in the playbook experiment log, checks the previous experiment's result, and retains the cycle in its Hindsight bank `marketing-lead`.

## Single data source

`marketing.gsc_search_analytics` (service account, `company_ops/seo_gsc_ingest.py`) is the only Search Console table. The OAuth `gsc_daily` path is removed; `sql/marketing_schema_v2_drop_gsc_daily.sql` drops the table (a human applies it).

## Boundaries

- Tools: `github-pr`, `reddit-publish`, `substack-publish`, `marketing-db-read`, `marketing-ingest`; no cluster access, no raw SQL.
- Git guard: pushes limited to `blog/<slug>` and `seo/<slug>` on the three configured site repos; never `main`, never force, never delete.
- Data access is a read-only DSN (`publisher_reader` role, kept under its existing name).

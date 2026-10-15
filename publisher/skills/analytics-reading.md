---
name: analytics-reading
description: Reading the marketing database safely
---
# Reading marketing data

- Use only: publisher db <name> [--days N --limit N]. List names: publisher db-queries.
- Refresh a source: publisher ingest <ga4|gsc|reddit|substack> [days]. It runs the same code as
  the nightly job; it needs no input from you and writes only validated rows.
- Check publisher db ingest-status first. skipped = no credential, unavailable = no API route,
  auth_failed = Substack cookie expired (a human must renew it), failed = see note.
- Never quote a number you did not read from a query result. Never write numbers yourself.
- Aggregates only exist here. If a task needs user-level data, say it is out of scope.

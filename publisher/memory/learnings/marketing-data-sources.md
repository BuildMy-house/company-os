# Learning: what each analytics source can and cannot give

- GA4 and Search Console: official read APIs; pages, sessions, queries, clicks.
- Reddit: official API, our own submissions only (score, comments). Policy approval unverified.
- Substack: unofficial and cookie-based; aggregates only; the cookie expires (auth_failed).
- Always check ingest-status before trusting a table; empty may mean skipped, not zero.

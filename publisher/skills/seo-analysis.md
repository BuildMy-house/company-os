---
name: seo-analysis
description: How to read analytics and the analysis tool to improve a post
---
# SEO analysis

1. Run: publisher analyze <post.md>. Fix every FAIL; weigh each WARN.
2. Pull evidence, do not guess: publisher db top-queries, publisher db top-pages.
3. Check the title and excerpt contain the main phrase people actually search (from top-queries).
4. One H2 per question the reader asks. Internal links to related posts. Descriptive link text.
5. Do not duplicate an existing post: if analyze reports phrase overlap, extend that post instead.
6. Report only what the data shows. State the source, date range and row counts. If a source
   was skipped or unavailable (publisher db ingest-status), say that analysis is untested for it.

# Marketing Plan

Marketing plan for Homely — content, campaigns, SEO, brand.

## Claim Board

Agents claim by editing ONLY their row, then commit `board: claim <TICKET-ID>`.
Status flow: `todo → claimed → in_progress → review → done`.
Only the manager sets `done`. Blocked: set status + reason.

| Ticket | Title | Deps | Owner dir | Track | Claimed-by | Status | Notes |
|--------|-------|------|-----------|-------|------------|--------|-------|
| M1 | *example: draft blog post on smart home design* | — | docs/marketing/ | content | — | todo | — |

## Track Legend

| Track | Scope | Owner |
|-------|-------|-------|
| content | Blog posts, landing pages, docs | marketing-dev |
| seo | Keyword research, optimization | marketing-dev |
| campaign | Email, social, paid ads | marketing-dev |
| brand | Brand guide, voice, visual identity | marketing-dev |
| analytics | Metrics, reporting, optimization | marketing-dev |

## Knowledge Base

Accumulated marketing knowledge lives in `knowledge/`:
- `knowledge/brand-guide.md` — Brand voice and visual guidelines
- `knowledge/seo-playbook.md` — SEO best practices for Homely
- `knowledge/content-calendar.md` — Upcoming content schedule
- `knowledge/analytics-baseline.md` — Performance benchmarks

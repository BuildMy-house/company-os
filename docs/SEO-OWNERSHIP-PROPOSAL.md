# SEO ownership proposal: CEO first, marketing worker later

Status: proposal for a human decision. Nothing here has been registered, deployed or changed in Hive.

## Finding

No marketing agent exists in Hive today. The only marketing-adjacent agents are the CEO (Hermes) and the designed-but-undeployed publisher (`docs/PUBLISHER.md`). Nobody currently owns search work, so the SEO playbook, `seo` skill, GSC ingest (`docs/SEO-GSC-INGEST.md`) and weekly loop (`docs/SEO-LEARNING-LOOP.md`) would have no runner.

## Recommendation

1. **Now: the CEO owns SEO through the `seo` skill.** It is seeded by `scripts/seed-ceo-behavior.sh` and run by a weekly cron, like the other CEO skills. The CEO reads `python -m company_ops.seo_report`, picks at most 3 experiments, appends them to the playbook experiment log through a PR (never self-merged) and files implementation work as Hive tickets per `docs/TICKET-STANDARD.md`. Reason: it needs no new infrastructure, and with a new site and near-zero data the weekly workload is small.
2. **Later, once the site has steady impressions: register a dedicated marketing worker.** Do not do this earlier; an idle always-on agent costs more than it returns.

## What a marketing worker registration needs

Per `docs/AGENT-CONTRACT.md` and `docs/TICKET-STANDARD.md`:

- A distinct pod and identity registering via `POST /agents/register` through the MCP member adapter, with `profile`, `modes` (execute) and `capabilities.tools` set from `AGENT_TOOLS`.
- Tools it should declare: `github-pr` (open PRs on the website and playbook), `marketing-db-read` (read `marketing.gsc_search_analytics`), `browser` (page checks). It should not declare `marketing-ingest`, `reddit-publish` or `substack-publish`; those stay with the publisher.
- A read-only database DSN (`hermes_analytics` or `publisher_reader` role; entry in `sql/roles.d`), with no write grant. The ingest CronJob keeps `marketing_ingest`.
- The `seo` skill mounted into its skills directory, and the playbook path readable.
- Git guard: pushes limited to `seo/*` branches on company-os and `blog/<slug>` or `seo/*` branches on the website; no push to `main`; merge stays human.
- Guidance and routing: tickets it should win carry `requires_tools: ["marketing-db-read"]`, so unrelated workers are not allocated them.

## Decisions needed

1. Approve CEO-owned SEO now (yes/no).
2. Decide when to register a marketing worker, with a suggested trigger of 4 consecutive weeks above a few hundred impressions per week.
3. Confirm the website push guard may allow `seo/*` branches.

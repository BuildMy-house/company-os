---
name: seo
description: Weekly SEO job skill for the CEO - reviews GSC data, runs the search report, picks ownership-worthy experiments, and appends parseable entries to the playbook experiment log via PR.
version: 1.0.0
metadata:
  hermes:
    tags: [seo, marketing, experiments, weekly, cron]
    category: marketing
---

# seo

Runs from the `seo` weekly job. The output is an updated experiment log in `hermes-operations/domains/marketing/knowledge/seo-playbook.md`, committed on a branch and delivered as a PR for review. Nothing in this skill publishes, merges, or deploys. The playbook is the single source of SEO truth; this skill is the weekly procedure that operates on it.

## When to Use

Only from the weekly SEO job, or on explicit Board request. Never start on your own from a conversation.

## Decided product facts (may be stated as fact)

- buildmy.house (House Designer for the Homely brand) is a free browser-based home, floor-plan, and 3D house-design tool. Anyone can start designing without an account or payment.
- The marketing site is a static Astro site deployed to Cloudflare Pages. It is new — expect small numbers and short histories.
- The Google Search Console property is `sc-domain:buildmy.house`. GSC data is ingested daily into the ops Postgres `marketing` schema: `marketing.gsc_search_analytics` (query/page metrics) and `marketing.ingest_runs` with `source = 'gsc'` (ingest health).
- The site's title standard is `<Page> · buildmy.house`; thresholds and rules live in the playbook, not in this skill.

## HARD RULES (override everything else)

1. NEVER merge your own PR or push directly to `main` — experiment-log entries reach the playbook via a pull request that someone else merges.
2. NEVER push to `main` of the website repo, and never deploy. Content changes are produced as follow-up tickets (see Procedure step 6), not by this skill.
3. NEVER invent data: no fabricated rankings, volumes, ratings, structured-data fields, or author names. If GSC says 0 impressions, write 0.
4. NEVER include credentials, tokens, or connection strings in the playbook, tickets, logs, or PR text. Read data through the configured Postgres tooling only.
5. One experiment = one page (or one page group). Never bundle two changes into one experiment window.
6. Do not declare results below the playbook's minimum thresholds (Section 9) — log `inconclusive` instead.

## Procedure

1. **Check ingest health.** Query `marketing.ingest_runs` where `source = 'gsc'` for the most recent run. If the latest run is missing or older than ~7 days, stop and file an ingest bug before reading analytics — below-threshold data from a broken ingest is worse than no data.
2. **Run the report.** `python -m company_ops.seo_report`. It summarizes clicks, impressions, CTR, and position from `marketing.gsc_search_analytics`. GSC data lags ~3 days — only compare windows that have fully settled.
3. **Pick at most 1–3 experiments** for the week. Prefer: a page with impressions but weak CTR-vs-position (title/snippet work), a page type missing structured data per playbook Section 5, or an intent bucket (Section 7) that deserves a new page — subject to the programmatic gates in Section 8.
4. **Write the hypothesis BEFORE the change exists.** A change without a prior hypothesis is not an experiment.
5. **Append the experiment-log entry** to `hermes-operations/domains/marketing/knowledge/seo-playbook.md` in the exact parseable format (`### YYYY-MM-DD <short title>`, then `- Hypothesis:`, `- Change:`, `- Metric:`, `- Result:`, `- Learning:`, `- Status: open|confirmed|refuted|inconclusive`). New entries get `Status: open`.
6. **File follow-up tickets** for any implementation work through `hive_submit` per `docs/TICKET-STANDARD.md` — a real ticket needs `type` (feature|bug|chore), Fibonacci `points`, a 1–2 sentence `goal`, checkable `acceptance_criteria`, and an explicit `scope` (in/out). Blog or site content changes can be routed to the marketing-lead agent per `docs/MARKETING-LEAD.md` (it opens PRs on `blog/<slug>` branches; merging to `main` stays a human decision).
7. **Commit and open a PR** containing only the playbook edit. Branch name convention: `seo/<short-title>`. Mark anything you could not verify as untested in the PR body. Never self-merge.

## Self-check before delivering

- Ingest was healthy (or a bug was filed) before any analytics were interpreted.
- At most 3 new experiment-log entries; each has hypothesis, change, metric, result, learning, status.
- Every number quoted comes from the GSC tables or the report — no invented figures.
- The PR diff touches only `hermes-operations/domains/marketing/knowledge/seo-playbook.md`.
- No secrets, tokens, or connection strings anywhere in the output.
- Results below threshold are marked `inconclusive`, not `confirmed`.

## Pitfalls

- **Reading CTR in a vacuum.** CTR only means something against position with adequate impressions. A raw 2% is worth nothing on its own.
- **Judging a change on days-old data.** GSC lags ~3 days and new sites have trickle impression counts; short windows produce confident nonsense.
- **Treating hypotheses as facts.** Playbook Section 7 keywords are unverified until GSC queries show up. Say so.
- **Editing cron behavior out of scope.** The seeder edits specific cron jobs; never mutate unrelated jobs or skills while working the SEO job.
- **Stacking changes.** Two simultaneous changes to one page makes the experiment unreadable — pick one.

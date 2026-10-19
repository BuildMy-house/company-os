# CEO crons — operations runbook

This covers the two Hermes cron jobs that drive the CEO's behavior
(hermes/SOUL.md defines who the CEO is): `ceo-status-update` and
`blog-draft-builder`. Their prompts, skills, and pre-run script are seeded
from this repo by `scripts/seed-ceo-behavior.sh`.

## What lives in git vs on the PVC

Git (source of truth, baked into the company-os image):

- `hermes/skills/ceo-status-update/`, `hermes/skills/blog-draft-builder/`,
  `hermes/skills/report-request/`, `hermes/skills/seo/` — the skill
  definitions. The `seo` skill backs the CEO's weekly SEO job (see
  `hermes-operations/domains/marketing/knowledge/seo-playbook.md`); it is
  seeded but no seo cron job is wired yet, so it runs on explicit request
  until that job exists.
- `hermes/scripts/ceo-status-prepare.sh` — pre-run script for
  ceo-status-update; it prints the final `{"wakeAgent": true, "context": ...}`
  JSON line the cron session reads (Hermes has no file/terminal tools).
- `memory/decisions/2026-10-18-pricing-free-core-render-credits.md` — the
  single authoritative pricing decision record.
- `scripts/seed-ceo-behavior.sh` — the idempotent seeder.
- `scripts/entrypoint.sh` — calls the seeder on every container start.

The PVC (`$HERMES_HOME`, `/opt/data` in the container) owns runtime state:

- `$HERMES_HOME/skills/`, `$HERMES_HOME/scripts/` — live copies of the above.
- `$HERMES_HOME/memory/` and `$HERMES_HOME/memories/` — Hermes memory,
  including `memory/decisions/` and `memory/reconcile-backup/`.
- `$HERMES_HOME/cron/jobs.json` — the cron jobs themselves (name, prompt,
  schedule, skills, enabled/paused, deliver). Never edited by hand while the
  gateway is running; use the `hermes cron` CLI.
- `$HERMES_HOME/cron/output/<job_id>/<timestamp>.md` — saved run output.
- `$HERMES_HOME/blog-draft-builder.mode` — the pause flag (see below).

## How the seed applies on restart

Every container start, `entrypoint.sh` runs `seed-ceo-behavior.sh`
(`|| true`; the seeder always exits 0 and never fails the boot). It:

1. Copies the four CEO skills from the image into `$HERMES_HOME/skills/`
   (only those four, overwritten; every other skill is left alone) and
   installs `ceo-status-prepare.sh` into `$HERMES_HOME/scripts/`.
2. Overwrites
   `$HERMES_HOME/memory/decisions/2026-10-18-pricing-free-core-render-credits.md`
   from the image copy — this one file is always authoritative.
3. Reconciles stale pricing memory: in `memories/MEMORY.md`,
   `memories/USER.md`, and every decisions note except the authoritative
   record, it removes entries that mention pricing/price/monetization/model
   AND say rejected / free-forever only / free only / no credits. Each
   changed file is backed up first (see rollback below) and each removed
   entry is logged (first 80 chars). It then ensures `MEMORY.md` holds
   exactly one pointer entry to the authoritative record, keeping the file
   under 2200 chars (over-cap additions are skipped with a WARN).
4. Finds the two cron jobs by name in `$HERMES_HOME/cron/jobs.json` (or
   `hermes cron list`) and re-points each job's prompt and skill (and
   ceo-status-update's pre-run script). The seeder NEVER creates jobs: the
   schedule and delivery target belong to the live jobs, which the Board or
   the CEO creates. Missing jobs are logged and picked up on a later start.

## The two crons

- `ceo-status-update`: periodic delta status report. The pre-run script
  builds the context (previous run, timestamps); the CEO reports only deltas
  since the previous run, keeps anything unverified in a separate Unverified
  section, and replies exactly `[SILENT]` when nothing changed. The CEO has
  no analytics tools by design: any metric is requested from the engineering
  agents via the `report-request` skill, never asserted from memory.
- `blog-draft-builder`: builds blog post drafts with a fact-check pass
  (unsourced figures or claims stay in the text but are labeled as
  hypotheses). Drafts only — it never publishes. Currently paused.

## Pausing / re-enabling blog-draft-builder

Pause state is a file flag plus the cron's own paused state:

- `$HERMES_HOME/blog-draft-builder.mode` — `paused` (seeded default) or
  `active`. Only the Board flips it; the seed never overwrites an existing
  flag.
- On every restart the seed reads the flag: anything other than exactly
  `active` runs `hermes cron pause <id>` (idempotent; already-paused errors
  are ignored); `active` runs `hermes cron resume <id>`.

Find the job id:

```
kubectl -n company-ops exec deploy/hermes-gateway -- hermes cron list
```

To re-enable once the publisher agent is deployed (run by an in-cluster
agent with container-manager/kubectl access):

```
kubectl -n company-ops exec deploy/hermes-gateway -- sh -c \
  'echo active > /opt/data/blog-draft-builder.mode && hermes cron resume <job_id>'
```

Both halves matter: the flag keeps future restarts from re-pausing the job,
and the resume activates it immediately.

## Rolling back the memory reconcile

Before the seed edits any memory file, it copies the original to
`$HERMES_HOME/memory/reconcile-backup/<UTC timestamp>/<filename>`. To
restore one file (e.g. after a bad removal):

```
kubectl -n company-ops exec deploy/hermes-gateway -- sh -c \
  'cp /opt/data/memory/reconcile-backup/<timestamp>/MEMORY.md /opt/data/memories/MEMORY.md'
```

Note the next restart re-runs the reconcile, so a restored contradicting
entry will be removed again unless the underlying decision record changes
(the authoritative record is never reconciled).

## Deploying changes

Live rollout of anything in this runbook requires a company-os image build
and rollout from an approved `prod` commit. Editing files in git does not
change the running CEO by itself; this change does not include a deploy.

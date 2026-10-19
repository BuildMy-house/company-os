# SEO Learning Loop

Analysis, every 2-3 days, of Google Search Console data to turn ranking/CTR movement into
documented learnings and follow-up tickets. Built by `company_ops/seo_report.py`,
exercised only from the company-os runtime.

## Sources

- `marketing` schema tables `marketing.gsc_search_analytics` and `marketing.ingest_runs` (`source = 'gsc'`)
  (owned and populated by `seo_gsc_ingest.py`; this module reads them
  read-only).
- Website git history (`src/pages`, `src/layouts`, `src/content`) from the
  checkout passed via `--repo-path`, used to correlate metric changes with
  content changes. Correlation only — no causation is claimed.

## Cycle run

The marketing-lead agent owns the cycle and runs the report job every 2-3 days (ingest stays daily). The report always compares the last 7 settled days with the prior 7, so consecutive runs overlap; judge experiments only past the playbook thresholds:

```sh
python -m company_ops.seo_report --week-ending 2026-10-17 --repo-path /srv/website --out seo-reports
```

- `--week-ending` defaults to the Saturday three days back (last complete week
  ending today−3d).
- Without `--week-ending`, current analytics are compared against the prior
  7-day window.
- The job writes `seo-reports/seo-report-YYYY-MM-DD.md` and appends an
  experiment-log entry (idempotent) to
  `hermes-operations/domains/marketing/knowledge/seo-playbook.md` under
  `## Experiment log`.
- Zero rows in both windows → prints "**No data yet.**" and exits 0 with no
  side effects.

## Follow-up tickets and PRs (opt-in)

- `--file-tickets`: files follow-up tickets (title rewrites, new page
  candidates) through `company_ops.hive_client.file_feedback_work`. Tickets are
  validated against the Hive field rules described in
  [`docs/TICKET-STANDARD.md`](TICKET-STANDARD.md) before submission; a failed
  write exits 4 with the report already on disk. Off by default.
- `--open-pr`: creates a `seo/learnings-YYYY-MM-DD` branch, commits the
  playbook entry, pushes and opens a PR flagged "human review required before
  merge". **Never self-merges** — a human approves and merges. Off by default,
  and never exercised outside mocked tests.
- `--dry-run`: prints the report (and tickets / PR commands when requested)
  without hitting the network, filing, or committing — writes only to stdout,
  except `--open-pr --dry-run` which additionally prints each git command it
  would run.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Report written (or "no data yet") |
| 3 | Database not configured (`ANALYTICS_DATABASE_URL` read-only reader, falling back to `MARKETING_INGEST_DATABASE_URL`) |
| 4 | Ticket filing failed |

`--file-tickets` and `--open-pr` never run automatically. They exist as opt-in
flags for scheduled runs where a human has explicitly accepted the side
effects.

## Scheduling (proposed, not applied)

A CronJob every 3 days at 06:00 UTC, modeled on
`k8s/base/dns-healthcheck/dns-healthcheck.yaml`:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: seo-learning-loop
  namespace: company-ops
spec:
  schedule: "0 6 */3 * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: seo-learning-loop
              image: localhost:30500/company-os
              command:
                - /opt/company-ops-venv/bin/python
                - -m
                - company_ops.seo_report
                - --out
                - /data/seo-reports
              env:
                - name: SEO_GSC_SITE_URL
                  value: "https://buildmy.house"
                - name: COMPANY_HIVE_URL
                  value: "http://hive:4100"
              envFrom:
                - secretRef:
                    name: company-ops-secrets
              resources:
                requests:
                  cpu: 50m
                  memory: 128Mi
                limits:
                  cpu: 250m
                  memory: 256Mi
          restartPolicy: Never
```

This snippet is a proposal only — it is **not** checked into `k8s/base/`, and
no deployment was applied. If adopted, add the image, secret, and PVC
mounts to match how the dns-healthcheck job is actually rolled out, and wire
`--repo-path` to the website checkout mounted into the pod. `--file-tickets`
and `--open-pr` should only be added to the command once the human approval
gate for learnings PRs is settled.

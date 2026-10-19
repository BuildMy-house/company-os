# Search Console ingestion (`seo-gsc-ingest`)

Daily Google Search Console ingestion into the `marketing` schema, with the
full dimension set (`date`, `query`, `page`, `country`, `device`) and a
16-month backfill. Runs as its own CronJob using a **service account**; the
existing OAuth-refresh-token fetcher in `company_ops/marketing_ingest.py`
is unchanged.

## What it does

`company_ops/seo_gsc_ingest.py`:

1. Reads `GSC_SERVICE_ACCOUNT_JSON` (the full service-account key JSON).
2. Signs an RS256 JWT for the **only** scope
   `https://www.googleapis.com/auth/webmasters.readonly` and exchanges it at
   `https://oauth2.googleapis.com/token`
   (`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`).
3. Calls
   `POST https://www.googleapis.com/webmasters/v3/sites/{siteUrl}/searchAnalytics/query`
   with `dimensions=[date,query,page,country,device]`, `rowLimit=25000`,
   `dataState=final`, paginating on `startRow` until a page is short.
4. Upserts into `marketing.gsc_search_analytics` (`ON CONFLICT` on the
   composite primary key), in batches, and records one
   `marketing.ingest_runs` row with `source='gsc'`.

Windows:

- **incremental (default)**: the last `GSC_DAYS` days (default 7) ending at
  `today(UTC) - GSC_LAG_DAYS` (default 3). Re-pulling is intentional: GSC
  revises late data, and the upsert makes it idempotent.
- **`--backfill`**: the last 16 months (GSC's retention limit), split into
  contiguous 30-day windows, one query per window with pagination inside.

An empty API response (`{"rows": []}` or no `rows` key) is a normal
zero-row run: `marketing.ingest_runs.status='ok'`, `rows_upserted=0`, exit 0.
Auth rejection (401/403 or a failed token exchange) is recorded as
`auth_failed` and exits non-zero. Other failures are `failed`, non-zero.

Environment variables:

| Variable | Required | Meaning |
|---|---|---|
| `GSC_SERVICE_ACCOUNT_JSON` | yes | service-account key JSON (secret) |
| `MARKETING_INGEST_DATABASE_URL` | yes (unless `--dry-run`) | DSN for role `marketing_ingest` |
| `GSC_SITE_URL` | no | default `sc-domain:buildmy.house` |
| `GSC_LAG_DAYS` | no | default `3` |
| `GSC_DAYS` | no | default `7` (incremental window) |

Nothing ever logs the key, the signed assertion, or the access token.
Errors carry only a URL, status code and a short body prefix.

## Overlap with `marketing_ingest.py`'s GSC fetcher

| | `marketing.gsc_daily` (existing) | `marketing.gsc_search_analytics` (this job) |
|---|---|---|
| Auth | OAuth refresh token (`GOOGLE_REFRESH_TOKEN`) | service account (RS256 JWT) |
| Dimensions | date, page, query | date, query, page, country, device |
| Window | trailing 7 days, daily | trailing 7 days daily + 16-month backfill |
| Schedule | `marketing-ingest` CronJob, 04:15 UTC | `seo-gsc-ingest` CronJob, 05:30 UTC |
| PK | `(date, site_url, page, query)` | `(site_url, date, query, page, country, device)` |

Both write from the same API and the aggregate numbers agree, but the row
grains differ, so they cannot be merged without losing `country`/`device`.
Recommendation: **keep `gsc_search_analytics` as the analytics source of
truth** (richer dimensions, backfill, service-account auth independent of a
refresh token that can be revoked) and retire the `gsc` source from
`marketing_ingest.py` once downstream consumers (`publisher_reader`,
`hermes_analytics`) have moved. That retirement is a separate change —
`marketing.gsc_daily` and `fetch_gsc` are deliberately left in place here.
Nothing in this ticket deletes them.

## Migration

The DDL is appended to `sql/marketing_schema.sql` (idempotent,
`CREATE TABLE IF NOT EXISTS`). Apply it as the role owner/admin:

```sh
psql -f sql/marketing_schema.sql "$MARKETING_INGEST_DATABASE_URL"
```

`marketing_ingest` already has `INSERT/UPDATE/SELECT` on all current and
future `marketing` tables, and `hermes_analytics` / `publisher_reader` have
`SELECT`; no role change is needed. The new table must exist before the
first run (the job does not create tables).

## Running it

```sh
# incremental (default 7 days, 3-day lag)
python -m company_ops.seo_gsc_ingest

# fetch only, no DB writes (counts printed as JSON)
python -m company_ops.seo_gsc_ingest --dry-run

# first-run backfill: 16 months in 30-day chunks
python -m company_ops.seo_gsc_ingest --backfill

# custom incremental window
python -m company_ops.seo_gsc_ingest --days 14
```

Local invocation needs `GSC_SERVICE_ACCOUNT_JSON` and
`MARKETING_INGEST_DATABASE_URL` in the environment (never in a committed
file).

## Proposed deployment changes (NOT applied)

Nothing below has been applied. These are the changes a human should review
and apply after the Infisical entries exist. The layout follows
`k8s/base/marketing-ingest/` (one directory, one `kustomization.yaml`, one
manifest) and the dedicated-Secret pattern in `k8s/base/publisher-secrets-sync/`.

### 1. Secrets: dedicated Secret, prod environment, `/seo`

Per `docs/SECRETS-INFISICAL.md`, each workload gets its **own** Secret with
exactly the keys it needs, synced from its own Infisical folder — never
`envFrom` of the broad `company-ops-secrets`. New folder
`/seo` in project **Build My house**
(`8806c2b0-73d2-4bea-8537-5b874c5ff592`), environment **prod**:

- `GSC_SERVICE_ACCOUNT_JSON` — the service-account key JSON.
- `MARKETING_INGEST_DATABASE_URL` — the `marketing_ingest` DSN. (It already
  exists in `/marketing-ingest`; copy the value into `/seo` so this Secret
  is self-contained and the job needs no second source.)
- `GSC_SITE_URL` — already present in `/marketing-ingest`; copy it too
  (non-secret, but keeps the folder's key set exact and the sync simple).

Note: `k8s/base/infisical-sync/infisical-sync.yaml` currently syncs only
environment **dev** for `/`, `/infra`, `/hermes`. The prod `/seo` folder is
therefore a **new** source; adding it to the broad `company-ops-sync`
would be wrong (it would leak the key into every workload's environment).
Use a separate `InfisicalStaticSecret`:

```yaml
# k8s/base/seo-gsc-ingest/seo-gsc-ingest-infisical-sync.yaml
# Syncs Infisical /seo (project "Build My house", environment prod) into its
# OWN Secret, seo-gsc-ingest-secrets, mounted only by the seo-gsc-ingest
# CronJob. The folder must contain exactly these keys (manual invariant; no
# test can see Infisical):
#   GSC_SERVICE_ACCOUNT_JSON      service-account key JSON (webmasters.readonly)
#   MARKETING_INGEST_DATABASE_URL DSN for marketing_ingest
#   GSC_SITE_URL                  e.g. sc-domain:buildmy.house
apiVersion: secrets.infisical.com/v1beta1
kind: InfisicalStaticSecret
metadata:
  name: seo-gsc-ingest-sync
  namespace: company-ops
spec:
  infisicalAuthRef:
    name: infisical-auth
    namespace: company-ops
  syncOptions:
    refreshInterval: 60s
  sources:
    - projectId: "8806c2b0-73d2-4bea-8537-5b874c5ff592"
      environmentSlug: prod
      secretPath: /seo
  targets:
    - name: seo-gsc-ingest-secrets
      namespace: company-ops
      kind: Secret
      creationPolicy: Owner
```

### 2. CronJob

Modeled on `k8s/base/dns-healthcheck/dns-healthcheck.yaml` (module
invocation, own ServiceAccount token disabled) and
`k8s/base/marketing-ingest/marketing-ingest.yaml` (dedicated Secret):

```yaml
# k8s/base/seo-gsc-ingest/seo-gsc-ingest.yaml
# Daily Search Console ingestion (company_ops/seo_gsc_ingest.py). Writes
# marketing.gsc_search_analytics as role marketing_ingest.
#
# NOT in any overlay on purpose: it needs Secret `seo-gsc-ingest-secrets`,
# which a human creates first (see docs/SEO-GSC-INGEST.md). Apply once by
# hand after that: kubectl apply -k k8s/base/seo-gsc-ingest
apiVersion: batch/v1
kind: CronJob
metadata:
  name: seo-gsc-ingest
  namespace: company-ops
spec:
  # Daily, after marketing-ingest (04:15) and the backups (03:00-03:45).
  schedule: "30 5 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          restartPolicy: Never
          automountServiceAccountToken: false
          containers:
            - name: ingest
              # Same image as marketing-ingest (Dockerfile.publisher carries
              # company_ops + the venv). Replace the digest with the current
              # built image before applying (docs/IMAGES.md).
              image: localhost:30500/company-os-publisher@sha256:ec3a00c848c3f0335c60f834dc11e5b963bc01c00dc8e0f676df1d9f75a2ad90
              imagePullPolicy: Always
              command: ["/opt/company-ops-venv/bin/python", "-m", "company_ops.seo_gsc_ingest"]
              envFrom:
                - secretRef:
                    name: seo-gsc-ingest-secrets
              resources:
                requests:
                  cpu: 50m
                  memory: 128Mi
                limits:
                  cpu: 250m
                  memory: 256Mi
```

```yaml
# k8s/base/seo-gsc-ingest/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - seo-gsc-ingest.yaml
  - seo-gsc-ingest-infisical-sync.yaml
```

The first run should be a manual backfill, not the daily CronJob. Run it as
a one-off Job (same image/Secret, `--backfill` appended):

```sh
kubectl -n company-ops create job seo-gsc-ingest-backfill \
  --image=<same image as the CronJob> \
  -- /opt/company-ops-venv/bin/python -m company_ops.seo_gsc_ingest --backfill
```

(That one-off Job needs the same `seo-gsc-ingest-secrets` env; if `kubectl
create job` cannot set `envFrom`, use a small one-off manifest mirroring the
CronJob's pod spec with the extra `--backfill` argument.)

## Status

Code, DDL, tests and this document are committed. **No cluster change has
been applied**: the `seo-gsc-ingest` CronJob, the `seo-gsc-ingest-secrets`
Secret and the prod `/seo` Infisical folder do not exist yet.

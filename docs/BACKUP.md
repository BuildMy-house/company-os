# Backups to Cloudflare R2

Two offsite backups run daily, both landing in the same R2 bucket under
distinct top-level prefixes:

| What | Prefix | Schedule | Where |
|---|---|---|---|
| Postgres `company` + `observer` schemas | `backups/` | 03:00 | k8s CronJob `postgres-backup` (`k8s/postgres-backup.yaml`) |
| hermes-data PVC (Hermes state/memory/sessions) | `hermes-data-backups/` | 03:30 | k8s CronJob `hermes-data-backup` (`k8s/hermes-data-backup.yaml`) |

Both CronJobs run `company_ops.backup` from the same digest-pinned image as
the app, with credentials from the `company-ops-secrets` secret (provisioned
from Infisical → `.env` via `scripts/k3s-local-up.sh`).

Retention: the newest `R2_BACKUP_KEEP_LAST` objects are kept **per prefix**
(default 7). The hermes prefix must stay top-level — the postgres retention
pass lists everything under `backups/`, so a nested prefix would be deleted
by it.

## Postgres dumps

Dumped with `pg_dump --format=plain` via the **`hermes_analytics`** read-only
role (`ANALYTICS_DATABASE_URL`). That role has SELECT across both schemas and
can write nothing — the least-privilege choice for a backup read
(see `sql/roles.d/`). Each run produces
`homely-backup-YYYYMMDD-HHMMSS.sql.gz`.

## hermes-data archives

`python -m company_ops.backup --hermes-data DIR` creates a gzipped tar of
`DIR`'s top level (stdlib `tarfile`) and uploads it as
`hermes-data-YYYYMMDD-HHMMSS.tar.gz`. The CronJob mounts the `hermes-data`
PVC read-only at `/mnt/hermes-data`. The PVC is RWO, so the backup pod pins
to the `hermes-gateway` node via pod affinity — fine on the current
single-node k3s, revisit before going multi-node.

## Manual run

```bash
pip install -e '.[backup]'
python -m company_ops.backup                        # postgres dump
python -m company_ops.backup --hermes-data /opt/data  # hermes-data archive
```

## Schedule

In-cluster the CronJobs above own the schedule. If you also want a host-level
cron (e.g. before the in-cluster image is deployed):

```cron
0 3 * * * /opt/company-ops/scripts/pg-backup.sh >> /var/log/pg-backup.log 2>&1
```

`scripts/pg-backup.sh` is a thin `set -euo pipefail` wrapper that cds to the
repo root and execs `python -m company_ops.backup`.

## Required environment variables

| Variable | Meaning |
|---|---|
| `ANALYTICS_DATABASE_URL` | Postgres DSN using the `hermes_analytics` read-only role (postgres backups only) |
| `R2_ENDPOINT` | S3-compatible endpoint, e.g. `https://<account>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | R2 bucket name (e.g. `homely-company`) |
| `R2_ACCESS_KEY_ID` | R2 access key |
| `R2_SECRET_ACCESS_KEY` | R2 secret key |
| `R2_BACKUP_KEEP_LAST` | most recent backups to retain **per prefix**; older ones are deleted (default 7) |

Any missing variable aborts the run with a `ValueError` naming the var —
backups never silently no-op.

## Verifying the offsite copies

A backup you haven't restored (or at least downloaded and opened) is a
hope, not a backup. `scripts/verify-offsite-backup.sh` checks the newest
object under each prefix is fresh (default: < 26 h old, ≥ 1 B) and, with
`--download-latest`, downloads and integrity-checks both (gzip + pg_dump
header / tar listing). See `docs/RESTORE.md` for the full drill.

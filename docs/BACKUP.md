# Postgres backups to Cloudflare R2

## What gets backed up

The `company` and `observer` schemas of the production Postgres database,
dumped with `pg_dump --format=plain` via the **`hermes_analytics`** read-only
role (`ANALYTICS_DATABASE_URL`). That role has SELECT across both schemas and
can write nothing — the least-privilege choice for a backup read
(see `company-ops/sql/roles.sql`).

Each run produces `homely-backup-YYYYMMDD-HHMMSS.sql.gz` under the
`backups/` prefix in the configured R2 bucket, then deletes all but the
newest `R2_BACKUP_KEEP_LAST` objects under that prefix.

## Manual run

```bash
pip install -e '.[backup]'
python -m company_ops.backup        # prints a JSON summary
```

## Schedule via cron

```cron
0 3 * * * /path/to/house_designer/company-ops/scripts/pg-backup.sh >> /var/log/pg-backup.log 2>&1
```

`scripts/pg-backup.sh` is a thin `set -euo pipefail` wrapper that cds to the
repo root and execs `python -m company_ops.backup`.

## Required environment variables

| Variable | Meaning |
|---|---|
| `ANALYTICS_DATABASE_URL` | Postgres DSN using the `hermes_analytics` read-only role |
| `R2_ENDPOINT` | S3-compatible endpoint, e.g. `https://<account>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | R2 bucket name (e.g. `homely-company`) |
| `R2_ACCESS_KEY_ID` | R2 access key |
| `R2_SECRET_ACCESS_KEY` | R2 secret key |
| `R2_BACKUP_KEEP_LAST` | number of most recent daily backups to retain in R2; older ones are deleted (default 30) |

Any missing variable aborts the run with a `ValueError` naming the var —
backups never silently no-op.

> **Note:** R2 credentials are not yet provisioned in production
> (see `company-ops/NAHAR-TODO.md`).

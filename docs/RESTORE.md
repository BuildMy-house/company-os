# Restore runbook (host move / disaster recovery)

How to bring the data back from R2 onto a new (or the same) host. Two data
sets, two restore paths:

- **Postgres** (`backups/*.sql.gz`) → restore with `scripts/restore-postgres.sh`
- **hermes-data PVC** (`hermes-data-backups/*.tar.gz`) → untar into the PVC

The drill assumes a fresh host provisioned with `scripts/k3s-local-up.sh`,
which already applies schemas + roles to a first-boot Postgres (from
`sql/*.sql`) and creates `company-ops-secrets` from `.env`.

## 0. Before touching anything: fetch + verify

```bash
# From any machine with the repo + python3/boto3 + R2_* env (from .env / Infisical)
scripts/verify-offsite-backup.sh --download-latest /tmp/restore-staging
```

This fails loudly if either backup is stale, truncated, or corrupt, and
leaves the newest of both in `/tmp/restore-staging`:

- `homely-backup-YYYYMMDD-HHMMSS.sql.gz`
- `hermes-data-YYYYMMDD-HHMMSS.tar.gz`

If verification fails, stop — restore from an older object in the bucket
(list with any S3 client); do not improvise.

## 1. Postgres: scratch-verify the dump first

```bash
POSTGRES_ADMIN_DSN='postgresql://postgres:<pw>@<new-host>:5432/postgres' \
  scripts/restore-postgres.sh /tmp/restore-staging/homely-backup-*.sql.gz
```

Restores into a throwaway `restore_verify` database and prints row counts
for every table, failing if any expected table (parsed from `sql/*.sql`) is
missing or `company.actions` / `observer.decisions` are empty. Takes about a
minute at current data sizes (dumps are ~1–2 MB compressed; budget minutes,
not hours). Drop the scratch DB when satisfied:

```bash
psql 'postgresql://postgres:<pw>@<new-host>:5432/postgres' -c 'DROP DATABASE restore_verify'
```

## 2. Postgres: live restore

**Fresh host:** the first-boot init already created the `company`/`observer`
schemas, so the dump's `CREATE TABLE`s would collide — pass `--drop-schemas`
(drops + the dump recreates them, data and all).

**Same host, re-restoring:** same flag, for the same reason.

```bash
# Stop writers first (all stateful consumers of the db)
kubectl -n company-ops scale deploy/hermes-gateway hive-coordinator --replicas=0

POSTGRES_ADMIN_DSN='postgresql://postgres:<pw>@<new-host>:5432/postgres' \
  scripts/restore-postgres.sh /tmp/restore-staging/homely-backup-*.sql.gz \
  --target live --yes --drop-schemas
```

The script: drops schemas → replays the dump (`psql -v ON_ERROR_STOP=1`, so
it aborts on the first error rather than half-restoring silently) →
re-runs `scripts/provision-db-roles.sh` (idempotent; restores GRANTs on the
restored tables and sets role passwords from env/Infisical `/hermes`; refuses
to run if any password is empty). Run it by hand if you restored elsewhere:

```bash
bash scripts/provision-db-roles.sh   # reads <ROLE>_PASSWORD keys from env/Infisical
```

Rollback: if the live restore fails midway, the schemas are in a broken
intermediate state — just re-run the same command (`--drop-schemas` resets
everything it touches). If you must return to the old host, it is untouched;
you only scaled writers down.

## 3. hermes-data PVC

```bash
# PVC is RWO: stop the writer before touching /opt/data
kubectl -n company-ops scale deploy/hermes-gateway --replicas=0

# Stage the archive into the (now stopped) gateway pod's volume
kubectl -n company-ops cp /tmp/restore-staging/hermes-data-*.tar.gz \
  deploy/hermes-gateway:/tmp/hermes-restore.tar.gz

# Unpack over the existing contents (tar.gz of /opt/data's top level)
kubectl -n company-ops exec deploy/hermes-gateway -- \
  sh -c 'rm -rf /opt/data/* && tar -xzf /tmp/hermes-restore.tar.gz -C /opt/data && rm /tmp/hermes-restore.tar.gz'

kubectl -n company-ops scale deploy/hermes-gateway --replicas=1
```

If the gateway pod can't run without its data (crash loop), use a temporary
pod mounting the same PVC read-write instead of the deploy pod.

## 4. Post-restore checks

```bash
# Backups still verify against the new reality
scripts/verify-offsite-backup.sh

# App is healthy and reading real data
kubectl -n company-ops get pods
kubectl -n company-ops logs deploy/hermes-gateway --tail=50
```

Spot-check a couple of row counts from the scratch-verify output in step 1
against the live db.

## Durations (current scale)

| Step | Rough time |
|---|---|
| Fetch + verify offsite | ~1 min |
| Scratch restore + sanity | ~1–2 min |
| Live restore + roles + passwords | ~2–3 min |
| hermes-data untar (≤ 5 Gi PVC) | minutes, size-dependent |

Total well under an hour. The long pole is provisioning the host itself
(`scripts/k3s-local-up.sh`), not the data.

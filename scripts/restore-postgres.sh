#!/usr/bin/env bash
# Restore a homely backup dump (from scripts/verify-offsite-backup.sh
# --download-latest) into postgres, with sanity checks. See docs/RESTORE.md.
#
# Usage: scripts/restore-postgres.sh DUMPFILE [--target scratch|live]
#            [--dsn DSN] [--scratch-db NAME] [--drop-schemas]
#            [--allow-empty-key-tables] [--dry-run]
#
#   scratch (default): restore into a throwaway database and verify table
#     presence + row counts + key-table contents. Never touches live data.
#   live: restore over the real database. REQUIRES --yes. Re-applies
#     sql/roles.sql afterwards (idempotent) so app roles keep their grants;
#     role passwords are set separately (scripts/04-set-role-passwords.sh).
#
# DSN: --dsn or POSTGRES_ADMIN_DSN env (a superuser/owner DSN like
# postgresql://postgres:PW@host:5432/postgres). Never printed.
set -euo pipefail

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

DUMPFILE=""
TARGET="scratch"
DSN="${POSTGRES_ADMIN_DSN:-}"
SCRATCH_DB="restore_verify"
DROP_SCHEMAS=0
ALLOW_EMPTY=0
YES=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --dsn) DSN="$2"; shift 2 ;;
    --scratch-db) SCRATCH_DB="$2"; shift 2 ;;
    --drop-schemas) DROP_SCHEMAS=1; shift ;;
    --allow-empty-key-tables) ALLOW_EMPTY=1; shift ;;
    --yes) YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *) if [[ -n "$DUMPFILE" ]]; then echo "unexpected arg: $1" >&2; exit 2; fi
       DUMPFILE="$1"; shift ;;
  esac
done

[[ -n "$DUMPFILE" ]] || { usage >&2; exit 2; }
[[ -f "$DUMPFILE" ]] || { echo "ERROR: dumpfile not found: $DUMPFILE" >&2; exit 2; }
case "$TARGET" in scratch|live) ;; *) echo "ERROR: --target must be scratch|live" >&2; exit 2 ;; esac
[[ -n "$DSN" ]] || { echo "ERROR: --dsn or POSTGRES_ADMIN_DSN required" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Masked DSN for display: never print credentials.
MASKED_DSN="$(python3 -c '
import re, sys
print(re.sub(r"(//[^:/@]+:)[^@]+(@)", r"\1***\2", sys.argv[1]))
' "$DSN")"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "dry-run: target=$TARGET dumpfile=$DUMPFILE dsn=$MASKED_DSN"
  echo "  scratch_db=$SCRATCH_DB drop_schemas=$DROP_SCHEMAS allow_empty_key_tables=$ALLOW_EMPTY"
  [[ $TARGET == live ]] && echo "  live: would require --yes, then restore + re-apply sql/roles.sql"
  exit 0
fi

if [[ $TARGET == live && $YES -ne 1 ]]; then
  echo "ERROR: live restore requires --yes (this overwrites the real database)" >&2
  exit 2
fi

PSQL=(psql --dbname="$DSN" -v ON_ERROR_STOP=1 --quiet)

# Expected tables, parsed from the schema SQL (roles.sql defines no tables).
EXPECTED_TABLES="$(grep -hoE 'CREATE TABLE (IF NOT EXISTS )?(company|observer)\.[a-z_]+' \
  "$REPO_ROOT"/sql/company_schema.sql "$REPO_ROOT"/sql/observer_schema.sql "$REPO_ROOT"/sql/pm_schema.sql \
  | sed -E 's/CREATE TABLE (IF NOT EXISTS )?//' | sort -u)"
[[ -n "$EXPECTED_TABLES" ]] || { echo "ERROR: no expected tables parsed from sql/" >&2; exit 2; }

if [[ $TARGET == scratch ]]; then
  # Derive admin "maintenance" DSN (db replaced by 'postgres') and the scratch DSN.
  MAINT_DSN="$(python3 -c '
import re, sys
print(re.sub(r"/[^/@]*$", "/postgres", sys.argv[1]))
' "$DSN")"
  SCRATCH_DSN="$(python3 -c '
import re, sys
print(re.sub(r"/[^/@]*$", "/" + sys.argv[1], sys.argv[2]))
' "$SCRATCH_DB" "$DSN")"
  psql --dbname="$MAINT_DSN" -v ON_ERROR_STOP=1 --quiet \
    -c "DROP DATABASE IF EXISTS $SCRATCH_DB" -c "CREATE DATABASE $SCRATCH_DB"
  echo "scratch database $SCRATCH_DB created; restoring..."
  TARGET_DSN="$SCRATCH_DSN"
else
  if [[ $DROP_SCHEMAS -eq 1 ]]; then
    echo "dropping schemas company, observer (cascade)..."
    psql --dbname="$DSN" -v ON_ERROR_STOP=1 --quiet \
      -c "DROP SCHEMA IF EXISTS company CASCADE" -c "DROP SCHEMA IF EXISTS observer CASCADE"
  fi
  echo "restoring into LIVE database..."
  TARGET_DSN="$DSN"
fi

if [[ "$DUMPFILE" == *.gz ]]; then
  gunzip -c "$DUMPFILE" | psql --dbname="$TARGET_DSN" -v ON_ERROR_STOP=1 --quiet
else
  psql --dbname="$TARGET_DSN" -v ON_ERROR_STOP=1 --quiet --file="$DUMPFILE"
fi

if [[ $TARGET == live ]]; then
  echo "re-applying sql/roles.sql (grants on restored tables)..."
  psql --dbname="$DSN" -v ON_ERROR_STOP=1 --quiet --file="$REPO_ROOT/sql/roles.sql"
fi

# --- Sanity checks -------------------------------------------------------
SANITY_DSN="$TARGET_DSN"
python3 - "$SANITY_DSN" "$EXPECTED_TABLES" "$ALLOW_EMPTY" <<'PYEOF'
import sys

import psycopg

dsn, expected_raw, allow_empty = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
expected = set(expected_raw.splitlines())

with psycopg.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT table_schema || '.' || table_name
        FROM information_schema.tables
        WHERE table_schema IN ('company', 'observer')
    """)
    actual = {row[0] for row in cur.fetchall()}

    missing = sorted(expected - actual)
    if missing:
        print("VERIFY FAILED: missing tables:", file=sys.stderr)
        for table in missing:
            print(f"  - {table}", file=sys.stderr)
        sys.exit(1)

    failures = []
    empty_key = []
    for table in sorted(actual):
        cur.execute('SELECT COUNT(*) FROM "' + table.replace('.', '"."') + '"')
        count = cur.fetchone()[0]
        print(f"  {table}: {count} rows")
        if table in ("company.actions", "observer.decisions") and count == 0:
            empty_key.append(table)

    if empty_key and not allow_empty:
        failures.append(
            "key tables empty: " + ", ".join(empty_key)
            + " (pass --allow-empty-key-tables if this dump is expected to be empty)"
        )
    if failures:
        print("VERIFY FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        sys.exit(1)

print("SANITY OK: all expected tables present with row counts above")
PYEOF

if [[ $TARGET == scratch ]]; then
  echo "scratch restore verified. Inspect with: psql \"$MASKED_DSN\" (db: $SCRATCH_DB)"
  echo "Drop it when done: psql (admin) -c 'DROP DATABASE $SCRATCH_DB'"
fi

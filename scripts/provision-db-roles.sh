#!/usr/bin/env bash
set -euo pipefail

# provision-db-roles.sh -- reconcile Postgres roles, passwords and grants with
# the declarative manifests in sql/roles.d/*.yaml. Idempotent.
#
# Usage: provision-db-roles.sh [--dry-run] [--only <role>]... [--manifest-dir DIR]
#
# Connection: standard libpq env (PGHOST, PGPORT, PGUSER, PGPASSWORD,
#   PGDATABASE; defaults user=postgres db=homely_company), or PG_ADMIN_DSN.
#   PGPASSWORD falls back to POSTGRES_PASSWORD. For the in-cluster database
#   use a port-forward (see NAHAR-TODO.md Group L).
# Passwords: each role's `password_key` from the environment; any still unset
#   are fetched from Infisical via scripts/fetch-infisical-secrets.js when
#   INFISICAL_* credentials are present (INFISICAL_SECRET_PATH defaults to /hermes).
# SAFETY: refuses (exit 2, before touching the database) if ANY selected role's
#   password is empty/missing/placeholder/not URL-safe. Passwords are only ever
#   sent over psql stdin and are never printed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST_DIR="$REPO_ROOT/sql/roles.d"
DRY_RUN=0
ONLY=()

die() { echo "ERROR: $1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --only) [ $# -ge 2 ] || die "--only needs a role name"; ONLY+=("$2"); shift ;;
    --manifest-dir) [ $# -ge 2 ] || die "--manifest-dir needs a path"; MANIFEST_DIR="$2"; shift ;;
    -h|--help) sed -n '4,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# --- Parse manifests (subset YAML) into tab-separated records -----------------
# R<TAB>name<TAB>login<TAB>password_key<TAB>dsn_key
# G<TAB>name<TAB>schema<TAB>tables(comma list or *)<TAB>privileges(comma list)
parse_manifest() {
  awk '
    function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
    function list(s) { gsub(/[\[\]]/, "", s); gsub(/[ \t]/, "", s); return s }
    function flush_grant() {
      if (gschema != "") printf "G\t%s\t%s\t%s\t%s\n", name, gschema, (gtables == "all" ? "*" : gtables), gprivs
      gschema = ""; gtables = ""; gprivs = ""
    }
    { sub(/[ \t]*#.*/, "") }
    /^[ \t]*$/ { next }
    /^[a-z_]+:/ {
      k = $0; sub(/:.*/, "", k); v = $0; sub(/^[^:]*:[ \t]*/, "", v); v = trim(v)
      if (k == "name") name = v
      else if (k == "login") login = v
      else if (k == "password_key") pk = v
      else if (k == "dsn_key") dk = v
      else if (k == "grants") ingrants = 1
      else { print "ERR\tunknown key " k > "/dev/stderr"; bad = 1 }
      next
    }
    ingrants && /^[ \t]+-?[ \t]*[a-z_]+:/ {
      line = $0
      if (line ~ /^[ \t]+-/) { flush_grant(); sub(/^[ \t]+-[ \t]*/, "", line) } else line = trim(line)
      k = line; sub(/:.*/, "", k); v = line; sub(/^[^:]*:[ \t]*/, "", v); v = trim(v)
      if (k == "schema") gschema = v
      else if (k == "tables") gtables = list(v)
      else if (k == "privileges") gprivs = list(v)
      else { print "ERR\tunknown grant key " k > "/dev/stderr"; bad = 1 }
      next
    }
    { print "ERR\tunparseable line: " $0 > "/dev/stderr"; bad = 1 }
    END {
      flush_grant()
      printf "R\t%s\t%s\t%s\t%s\n", name, login, pk, dk
      exit bad
    }
  ' "$1"
}

shopt -s nullglob
files=("$MANIFEST_DIR"/*.yaml)
[ ${#files[@]} -gt 0 ] || die "no manifests in $MANIFEST_DIR"

declare -A LOGIN PWKEY DSNKEY
ROLES=()
GRANTS=()   # "role<TAB>schema<TAB>tables<TAB>privs"
for f in "${files[@]}"; do
  recs="$(parse_manifest "$f")" || die "cannot parse $f"
  while IFS=$'\t' read -r kind a b c d; do
    if [ "$kind" = R ]; then
      [[ "$a" =~ ^[a-z_][a-z0-9_]*$ ]] || die "$f: invalid role name '$a'"
      [ "$a" = "$(basename "$f" .yaml)" ] || die "$f: name '$a' must match the filename"
      [ "$b" = true ] || [ "$b" = false ] || die "$f: login must be true or false"
      [[ "$c" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "$f: password_key must be an UPPER_SNAKE env name"
      [[ "$d" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "$f: dsn_key must be an UPPER_SNAKE env name"
      ROLES+=("$a"); LOGIN[$a]="$b"; PWKEY[$a]="$c"; DSNKEY[$a]="$d"
    else
      [[ "$b" =~ ^[a-z_][a-z0-9_]*$ ]] || die "$f: invalid schema '$b'"
      [[ "$c" = '*' || "$c" =~ ^[a-z_][a-z0-9_]*(,[a-z_][a-z0-9_]*)*$ ]] || die "$f: invalid tables '$c'"
      [[ "$d" =~ ^(SELECT|INSERT|UPDATE|DELETE|TRUNCATE)(,(SELECT|INSERT|UPDATE|DELETE|TRUNCATE))*$ ]] \
        || die "$f: invalid privileges '$d'"
      GRANTS+=("$a"$'\t'"$b"$'\t'"$c"$'\t'"$d")
    fi
  done <<< "$recs"
done

# --- Select roles --------------------------------------------------------------
SELECTED=()
if [ ${#ONLY[@]} -gt 0 ]; then
  for r in "${ONLY[@]}"; do
    [ -n "${LOGIN[$r]+x}" ] || die "--only: no manifest for role '$r'"
    SELECTED+=("$r")
  done
else
  SELECTED=("${ROLES[@]}")
fi

# --- Resolve passwords (env first, then Infisical for anything unset) ---------
declare -A PW DSNVAL
needs_fetch=0
for r in "${SELECTED[@]}"; do
  [ -n "${!PWKEY[$r]:-}" ] || needs_fetch=1
done
INFISICAL_JSON=""
if [ "$needs_fetch" = 1 ] && [ -n "${INFISICAL_UNIVERSAL_AUTH_CLIENT_ID:-}" ] \
   && [ -n "${INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET:-}" ]; then
  echo "==> Fetching missing role passwords from Infisical (path ${INFISICAL_SECRET_PATH:-/hermes})" >&2
  INFISICAL_JSON="$(INFISICAL_SECRET_PATH="${INFISICAL_SECRET_PATH:-/hermes}" \
    node "$SCRIPT_DIR/fetch-infisical-secrets.js" --json)" || die "Infisical fetch failed" 2
fi
infisical_get() { # key -> value on stdout (empty if absent)
  [ -n "$INFISICAL_JSON" ] || return 0
  printf '%s' "$INFISICAL_JSON" | node -e '
    let s=""; process.stdin.on("data",d=>s+=d).on("end",()=>{
      const m=JSON.parse(s).find(x=>x.secretKey===process.argv[1]);
      if(m&&m.secretValue) process.stdout.write(m.secretValue);
    });' "$1"
}

bad=0
for r in "${SELECTED[@]}"; do
  key="${PWKEY[$r]}"
  val="${!key:-}"
  [ -n "$val" ] || val="$(infisical_get "$key")"
  if [ -z "$val" ]; then
    echo "REFUSING: password for role '$r' ($key) is empty or missing" >&2; bad=1; continue
  fi
  if [[ "$val" == CHANGE_ME* ]] || ! [[ "$val" =~ ^[A-Za-z0-9._~-]+$ ]]; then
    echo "REFUSING: password for role '$r' ($key) is a placeholder or not URL-safe ([A-Za-z0-9._~-]+)" >&2
    bad=1; continue
  fi
  PW[$r]="$val"
  dkey="${DSNKEY[$r]}"
  dval="${!dkey:-}"
  [ -n "$dval" ] || dval="$(infisical_get "$dkey")"
  DSNVAL[$r]="$dval"
done
[ "$bad" = 0 ] || die "refusing to run: fix the passwords above; database not touched" 2

# --- Generate SQL --------------------------------------------------------------
emit_sql() { # $1 = role, $2 = password literal (real or <redacted>)
  local r="$1" pw="$2" line schema tables privs t denied p list
  local lg="LOGIN"; [ "${LOGIN[$r]}" = true ] || lg="NOLOGIN"
  echo "-- role $r"
  echo "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$r') THEN CREATE ROLE \"$r\" $lg; END IF; END \$\$;"
  echo "ALTER ROLE \"$r\" WITH $lg PASSWORD '$pw';"
  for line in "${GRANTS[@]}"; do
    IFS=$'\t' read -r gr schema tables privs <<< "$line"
    [ "$gr" = "$r" ] || continue
    echo "GRANT USAGE ON SCHEMA \"$schema\" TO \"$r\";"
    denied=""
    for p in INSERT UPDATE DELETE TRUNCATE; do
      [[ ",$privs," == *",$p,"* ]] || denied="${denied:+$denied, }$p"
    done
    if [ "$tables" = '*' ]; then
      echo "GRANT ${privs//,/, } ON ALL TABLES IN SCHEMA \"$schema\" TO \"$r\";"
      echo "ALTER DEFAULT PRIVILEGES IN SCHEMA \"$schema\" GRANT ${privs//,/, } ON TABLES TO \"$r\";"
      echo "REVOKE $denied ON ALL TABLES IN SCHEMA \"$schema\" FROM \"$r\";"
      echo "ALTER DEFAULT PRIVILEGES IN SCHEMA \"$schema\" REVOKE $denied ON TABLES FROM \"$r\";"
    else
      list=""
      for t in ${tables//,/ }; do list="${list:+$list, }\"$schema\".\"$t\""; done
      echo "GRANT ${privs//,/, } ON $list TO \"$r\";"
      echo "REVOKE $denied ON $list FROM \"$r\";"
    fi
  done
}

if [ "$DRY_RUN" = 1 ]; then
  echo "==> DRY RUN: SQL that would be applied (passwords redacted); database not touched"
  for r in "${SELECTED[@]}"; do emit_sql "$r" '<redacted>'; done
  echo "==> Dry run OK: ${#SELECTED[@]} role(s), all passwords present and valid"
  exit 0
fi

# --- Apply (one transaction per role) -----------------------------------------
export PGUSER="${PGUSER:-postgres}"
export PGDATABASE="${PGDATABASE:-homely_company}"
[ -z "${PGPASSWORD:-}" ] && [ -n "${POSTGRES_PASSWORD:-}" ] && export PGPASSWORD="$POSTGRES_PASSWORD"
ADMIN=(psql -X -q -v ON_ERROR_STOP=1 --single-transaction)
[ -z "${PG_ADMIN_DSN:-}" ] || ADMIN+=(--dbname "$PG_ADMIN_DSN")

for r in "${SELECTED[@]}"; do
  echo "==> Provisioning role $r"
  emit_sql "$r" "${PW[$r]}" | "${ADMIN[@]}" >/dev/null || die "provisioning $r failed (rolled back)"
done

# --- Verify: log in as each role with its password ----------------------------
fail=0
for r in "${SELECTED[@]}"; do
  [ "${LOGIN[$r]}" = true ] || continue
  who="$(PGUSER="$r" PGPASSWORD="${PW[$r]}" psql -X -q -t -A ${PG_ADMIN_DSN:+--dbname "$PG_ADMIN_DSN"} \
        -c 'SELECT current_user' 2>/dev/null)" || who=""
  if [ "$who" = "$r" ]; then echo "    login OK: $r"; else echo "    LOGIN FAILED: $r" >&2; fail=1; fi
  # Catch the "blank password wrote a broken DSN" failure: the stored DSN must embed this password.
  if [ -n "${DSNVAL[$r]}" ]; then
    case "${DSNVAL[$r]}" in
      postgresql://"$r":"${PW[$r]}"@*) echo "    DSN ${DSNKEY[$r]} matches" ;;
      *) echo "    DSN MISMATCH: ${DSNKEY[$r]} does not embed role $r's password -- update it in Infisical/Secret" >&2; fail=1 ;;
    esac
  else
    echo "    note: ${DSNKEY[$r]} not available to check; ensure it embeds this password"
  fi
done
[ "$fail" = 0 ] || die "verification failed" 3
echo "==> Done: ${#SELECTED[@]} role(s) reconciled and verified"

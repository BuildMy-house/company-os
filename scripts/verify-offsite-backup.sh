#!/usr/bin/env bash
# Verify the offsite (R2) backups are fresh and intact — the check that makes
# the backups worth having. Exit non-zero if anything fails.
#
# Checks the newest object under each backup prefix (backups/ for the
# postgres dumps, hermes-data-backups/ for the hermes-data archives):
#   - age <= --max-age-hours (default 26: daily cron + slack)
#   - size >= --min-size-bytes (default 1)
# With --download-latest [DIR], also downloads both newest objects and
# verifies gzip integrity + dump/archive structure.
#
# Env (same as company_ops.backup, e.g. exported from .env or
# company-ops-secrets): R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID,
# R2_SECRET_ACCESS_KEY. Never prints secret values.
#
# Requires python3 with boto3 (the project's only S3 dependency).
set -euo pipefail

MAX_AGE_HOURS=26
MIN_SIZE_BYTES=1
DOWNLOAD_DIR=""
DO_DOWNLOAD=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-age-hours) MAX_AGE_HOURS="$2"; shift 2 ;;
    --min-size-bytes) MIN_SIZE_BYTES="$2"; shift 2 ;;
    --download-latest)
      DO_DOWNLOAD=1
      if [[ $# -gt 1 && ! "$2" =~ ^-- ]]; then DOWNLOAD_DIR="$2"; shift; fi
      shift
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [[ $DRY_RUN -eq 1 ]]; then
  echo "dry-run: would check newest object under backups/ and hermes-data-backups/"
  echo "  max_age_hours=$MAX_AGE_HOURS min_size_bytes=$MIN_SIZE_BYTES download=$DO_DOWNLOAD"
  exit 0
fi

if ! python3 -c 'import boto3' 2>/dev/null; then
  echo "ERROR: python3 + boto3 required (pip install boto3)" >&2
  exit 2
fi

DOWNLOAD_DIR="${DOWNLOAD_DIR:-$(mktemp -d /tmp/offsite-verify.XXXXXX)}"
mkdir -p "$DOWNLOAD_DIR"

python3 - "$MAX_AGE_HOURS" "$MIN_SIZE_BYTES" "$DOWNLOAD_DIR" <<'PYEOF'
import gzip
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone

import boto3

max_age_hours = float(sys.argv[1])
min_size_bytes = int(sys.argv[2])
download_dir = sys.argv[3]

missing = [v for v in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
           if not os.environ.get(v)]
if missing:
    print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
    sys.exit(2)

client = boto3.client(
    "s3",
    endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
)
bucket = os.environ["R2_BUCKET"]

failures = []


def newest(prefix):
    objects = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    if not objects:
        return None
    return max(objects, key=lambda o: o["LastModified"])


def check_gzip_integrity(path):
    with gzip.open(path, "rb") as f:
        while f.read(1024 * 1024):
            pass


def verify_postgres(path):
    # Dumps are plain-format SQL (pg_dump --format=plain); pg_restore --list
    # cannot read text dumps, so verify gzip integrity + dump header instead.
    check_gzip_integrity(path)
    with gzip.open(path, "rt", errors="replace") as f:
        head = f.read(4096)
    if "PostgreSQL database dump" not in head:
        raise ValueError("not a pg_dump text file (no dump header)")


def verify_hermes(path):
    check_gzip_integrity(path)
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
    if not names:
        raise ValueError("hermes archive is empty")


for label, prefix in (("postgres dump", "backups/"),
                      ("hermes-data archive", "hermes-data-backups/")):
    obj = newest(prefix)
    if obj is None:
        failures.append(f"{label}: NO OBJECTS under {prefix}")
        continue
    key, size = obj["Key"], obj["Size"]
    age = datetime.now(timezone.utc) - obj["LastModified"]
    ok_age = age <= timedelta(hours=max_age_hours)
    ok_size = size >= min_size_bytes
    print(f"{label}: {key}  size={size}B  age={age.total_seconds()/3600:.1f}h")
    if not ok_age:
        failures.append(f"{label}: newest backup is {age.total_seconds()/3600:.1f}h old (max {max_age_hours}h)")
    if not ok_size:
        failures.append(f"{label}: newest backup is {size}B (min {min_size_bytes}B)")
    if ok_age and ok_size:
        local = os.path.join(download_dir, os.path.basename(key))
        client.download_file(bucket, key, local)
        try:
            verify_postgres(local) if prefix == "backups/" else verify_hermes(local)
            print(f"{label}: downloaded + integrity OK -> {local}")
        except Exception as exc:
            failures.append(f"{label}: integrity check failed for {key}: {exc}")

if failures:
    print("\nVERIFY FAILED:", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    sys.exit(1)
print("\nVERIFY OK: both backup prefixes fresh and intact")
PYEOF

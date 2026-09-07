"""Scheduled pg_dump backup of company+observer schemas to Cloudflare R2.

Pure module — no dependency on Ledger/ObserverWriter. Run directly via
``python -m company_ops.backup`` or import ``run_backup`` from scripts/cron.
"""

from __future__ import annotations

import gzip
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import boto3

SCHEMAS = ["company", "observer"]
BACKUP_PREFIX = "backups/"


def run_pg_dump(dsn: str, dump_path: str, schemas: list[str] = SCHEMAS) -> None:
    """Dump the given schemas to a plain-SQL file at dump_path via pg_dump."""
    cmd = [
        "pg_dump",
        f"--dbname={dsn}",
        "--format=plain",
        "--no-owner",
        "--no-privileges",
        "--file",
        dump_path,
    ]
    for schema in schemas:
        cmd.append(f"--schema={schema}")
    subprocess.run(cmd, check=True)


def compress_file(path: str) -> str:
    """Gzip the file at path to path + '.gz' (stdlib gzip), delete original.

    Returns the .gz path. Intended for .sql dumps; any file works.
    """
    gz_path = path + ".gz"
    with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
    os.remove(path)
    return gz_path


def upload_to_r2(
    local_path: str,
    bucket: str,
    key: str,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
) -> None:
    """Upload local_path to the S3-compatible endpoint (Cloudflare R2)."""
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )
    client.upload_file(local_path, bucket, key)


def enforce_retention(
    bucket: str,
    prefix: str,
    keep_last: int,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
) -> list[str]:
    """Delete all objects under prefix except the keep_last most recent.

    Sorts by LastModified descending; returns deleted keys (oldest first).
    """
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
    )
    objects: list[dict] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(page.get("Contents", []))
    objects.sort(key=lambda obj: obj["LastModified"], reverse=True)
    # objects[keep_last:] are the oldest; report/delete them oldest-first.
    deleted = [obj["Key"] for obj in reversed(objects[keep_last:])]
    for key in deleted:
        client.delete_object(Bucket=bucket, Key=key)
    return deleted


def _require(value: str | None, env_var: str) -> str:
    if value is None:
        value = os.environ.get(env_var)
    if not value:
        raise ValueError(f"{env_var} is not set — required for backup")
    return value


def run_backup(
    pg_dsn: str | None = None,
    r2_endpoint: str | None = None,
    r2_bucket: str | None = None,
    r2_access_key_id: str | None = None,
    r2_secret_access_key: str | None = None,
    keep_last: int | None = None,
    work_dir: str | None = None,
) -> dict:
    """Full backup pipeline: pg_dump -> gzip -> R2 upload -> retention.

    Any arg left None is read from os.environ (ANALYTICS_DATABASE_URL,
    R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
    R2_BACKUP_KEEP_LAST). Raises ValueError naming the missing var.
    """
    pg_dsn = _require(pg_dsn, "ANALYTICS_DATABASE_URL")
    r2_endpoint = _require(r2_endpoint, "R2_ENDPOINT")
    r2_bucket = _require(r2_bucket, "R2_BUCKET")
    access_key_id = _require(r2_access_key_id, "R2_ACCESS_KEY_ID")
    secret_access_key = _require(r2_secret_access_key, "R2_SECRET_ACCESS_KEY")
    if keep_last is None:
        keep_last = int(os.environ.get("R2_BACKUP_KEEP_LAST", "30"))
    if keep_last < 0:
        raise ValueError("keep_last must be >= 0")

    work_dir_path = Path(work_dir or tempfile.gettempdir())
    work_dir_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"homely-backup-{stamp}.sql"
    dump_path = work_dir_path / filename

    run_pg_dump(pg_dsn, str(dump_path))
    gz_path = compress_file(str(dump_path))
    key = f"{BACKUP_PREFIX}{filename}.gz"
    upload_to_r2(gz_path, r2_bucket, key, r2_endpoint, access_key_id, secret_access_key)
    deleted_keys = enforce_retention(
        r2_bucket, BACKUP_PREFIX, keep_last, r2_endpoint, access_key_id, secret_access_key
    )

    bytes_uploaded = os.path.getsize(gz_path)
    os.remove(gz_path)
    return {
        "uploaded_key": key,
        "deleted_keys": deleted_keys,
        "bytes_uploaded": bytes_uploaded,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(run_backup(), default=str))

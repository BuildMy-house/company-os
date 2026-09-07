import gzip
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from company_ops import backup

_REQUIRED_ENV = {
    "ANALYTICS_DATABASE_URL": "postgresql://u:p@localhost:5432/db",
    "R2_ENDPOINT": "https://example.r2.cloudflarestorage.com",
    "R2_BUCKET": "homely-company",
    "R2_ACCESS_KEY_ID": "key-id",
    "R2_SECRET_ACCESS_KEY": "secret",
    "R2_BACKUP_KEEP_LAST": "30",
}


class CompressFileTests(unittest.TestCase):
    def test_compresses_deletes_original_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dump.sql")
            with open(path, "wb") as f:
                f.write(b"CREATE TABLE x ();\n" * 100)
            gz_path = backup.compress_file(path)
            self.assertTrue(gz_path.endswith(".gz"))
            self.assertTrue(os.path.exists(gz_path))
            self.assertFalse(os.path.exists(path))
            with gzip.open(gz_path, "rb") as f:
                self.assertEqual(f.read(), b"CREATE TABLE x ();\n" * 100)


class UploadToR2Tests(unittest.TestCase):
    def test_uploads_via_boto3(self):
        with mock.patch("company_ops.backup.boto3.client") as client_factory:
            backup.upload_to_r2(
                "dump.sql.gz", "homely-company", "backups/dump.sql.gz",
                "https://example.r2.cloudflarestorage.com", "key-id", "secret",
            )
        client_factory.assert_called_once_with(
            "s3",
            endpoint_url="https://example.r2.cloudflarestorage.com",
            aws_access_key_id="key-id",
            aws_secret_access_key="secret",
        )
        client_factory.return_value.upload_file.assert_called_once_with(
            "dump.sql.gz", "homely-company", "backups/dump.sql.gz"
        )


class EnforceRetentionTests(unittest.TestCase):
    ENDPOINT = "https://example.r2.cloudflarestorage.com"

    @staticmethod
    def _objects(count):
        base = datetime(2026, 9, 1, tzinfo=timezone.utc)
        return [
            {"Key": f"backups/dump-{i:02d}.sql.gz",
             "LastModified": base + timedelta(hours=i)}
            for i in range(count)
        ]

    def _client_with(self, objects):
        client = mock.MagicMock()
        paginator = mock.MagicMock()
        paginator.paginate.return_value = [{"Contents": objects}] if objects else [{}]
        client.get_paginator.return_value = paginator
        return client

    def _enforce(self, client, keep_last):
        with mock.patch("company_ops.backup.boto3.client", return_value=client):
            return backup.enforce_retention(
                "homely-company", "backups/", keep_last,
                self.ENDPOINT, "key-id", "secret",
            )

    def test_deletes_oldest_beyond_keep_last(self):
        objects = self._objects(35)
        client = self._client_with(objects)
        deleted = self._enforce(client, 30)
        # Objects sorted oldest-first are dump-00..dump-04; those get deleted.
        self.assertEqual(
            deleted,
            [f"backups/dump-{i:02d}.sql.gz" for i in range(5)],
        )
        self.assertEqual(client.delete_object.call_count, 5)
        for call, expected_key in zip(
            client.delete_object.call_args_list, deleted
        ):
            self.assertEqual(
                call.kwargs, {"Bucket": "homely-company", "Key": expected_key}
            )

    def test_deletes_nothing_within_limit(self):
        client = self._client_with(self._objects(3))
        deleted = self._enforce(client, 30)
        self.assertEqual(deleted, [])
        client.delete_object.assert_not_called()


class RunBackupTests(unittest.TestCase):
    def test_raises_value_error_when_dsn_missing(self):
        env = {k: v for k, v in _REQUIRED_ENV.items()
               if k != "ANALYTICS_DATABASE_URL"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(backup, "run_pg_dump"), \
             mock.patch.object(backup, "compress_file", return_value="d.gz"):
            with self.assertRaises(ValueError) as ctx:
                backup.run_backup()
        self.assertIn("ANALYTICS_DATABASE_URL", str(ctx.exception))

    def test_raises_value_error_when_r2_bucket_missing(self):
        env = {k: v for k, v in _REQUIRED_ENV.items() if k != "R2_BUCKET"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(backup, "run_pg_dump"), \
             mock.patch.object(backup, "compress_file", return_value="d.gz"):
            with self.assertRaises(ValueError) as ctx:
                backup.run_backup()
        self.assertIn("R2_BUCKET", str(ctx.exception))

    def test_success_returns_summary_dict(self):
        with mock.patch.dict(os.environ, _REQUIRED_ENV, clear=True), \
             mock.patch("company_ops.backup.run_pg_dump") as dump, \
             mock.patch("company_ops.backup.compress_file",
                        return_value="dump.sql.gz") as compress, \
             mock.patch("company_ops.backup.upload_to_r2") as upload, \
             mock.patch("company_ops.backup.enforce_retention",
                        return_value=["backups/old.sql.gz"]) as retention, \
             mock.patch("company_ops.backup.os.path.getsize",
                        return_value=1234), \
             mock.patch("company_ops.backup.os.remove") as remove:
            summary = backup.run_backup()

        dump.assert_called_once()
        compress.assert_called_once()
        upload.assert_called_once()
        retention.assert_called_once()
        remove.assert_called_once_with("dump.sql.gz")
        self.assertEqual(summary["deleted_keys"], ["backups/old.sql.gz"])
        self.assertEqual(summary["bytes_uploaded"], 1234)
        self.assertRegex(
            summary["uploaded_key"],
            r"^backups/homely-backup-\d{8}-\d{6}\.sql\.gz$",
        )
        # keep_last env var is honored
        retention.assert_called_once_with(
            "homely-company", "backups/", 30,
            "https://example.r2.cloudflarestorage.com", "key-id", "secret",
        )


if __name__ == "__main__":
    unittest.main()

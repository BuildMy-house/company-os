"""Tests for scripts/provision-db-roles.sh and sql/roles.d manifests.

Refusal/dry-run tests need no database. Idempotency/grant tests need Docker
(postgres:16-alpine, ephemeral container on a random port) and psql; they skip
otherwise. Nothing here touches a live cluster, database or Infisical.
"""
import os
import re
import shutil
import subprocess
import time
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "provision-db-roles.sh")
MANIFESTS = os.path.join(REPO, "sql", "roles.d")

PASSWORDS = {
    "COMPANY_PASSWORD": "pwcompany-0123456789",
    "OBSERVER_PASSWORD": "pwobserver-0123456789",
    "ANALYTICS_PASSWORD": "pwanalytics-0123456789",
    "PM_AGENT_WRITER_PASSWORD": "pwpmagent-0123456789",
}
ROLES = ["hermes_company", "hermes_observer_writer", "hermes_analytics", "pm_agent_writer"]


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("INFISICAL_", "PG")) and not k.endswith("_PASSWORD")
           and not k.endswith("_DATABASE_URL")}
    env.update(extra)
    return env


def run(args, env):
    return subprocess.run(["bash", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=120)


class ManifestTests(unittest.TestCase):
    def test_all_existing_roles_have_manifests(self):
        names = sorted(f[:-5] for f in os.listdir(MANIFESTS) if f.endswith(".yaml"))
        self.assertEqual(names, sorted(ROLES))

    def test_manifest_keys_present(self):
        for r in ROLES:
            text = open(os.path.join(MANIFESTS, r + ".yaml")).read()
            for key in ("name:", "login:", "password_key:", "dsn_key:", "grants:"):
                self.assertIn(key, text, f"{r} missing {key}")


class RefusalTests(unittest.TestCase):
    def test_refuses_when_any_password_missing(self):
        env = clean_env(**{k: v for k, v in PASSWORDS.items() if k != "PM_AGENT_WRITER_PASSWORD"})
        p = run([], env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("pm_agent_writer", p.stderr)
        self.assertIn("database not touched", p.stderr)

    def test_refuses_blank_password(self):
        env = clean_env(**{**PASSWORDS, "PM_AGENT_WRITER_PASSWORD": ""})
        p = run([], env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("empty or missing", p.stderr)

    def test_refuses_placeholder_and_unsafe(self):
        for bad in ("CHANGE_ME_PM_AGENT_WRITER_PASSWORD", "has'quote", "has space"):
            env = clean_env(**{**PASSWORDS, "PM_AGENT_WRITER_PASSWORD": bad})
            p = run(["--dry-run"], env)
            self.assertEqual(p.returncode, 2, bad)
            self.assertNotIn(bad, p.stdout + p.stderr)

    def test_only_checks_selected_role(self):
        env = clean_env(PM_AGENT_WRITER_PASSWORD=PASSWORDS["PM_AGENT_WRITER_PASSWORD"])
        p = run(["--dry-run", "--only", "pm_agent_writer"], env)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_unknown_only_role(self):
        self.assertNotEqual(run(["--dry-run", "--only", "nope"], clean_env(**PASSWORDS)).returncode, 0)

    def test_dry_run_never_prints_passwords(self):
        p = run(["--dry-run"], clean_env(**PASSWORDS))
        self.assertEqual(p.returncode, 0, p.stderr)
        for v in PASSWORDS.values():
            self.assertNotIn(v, p.stdout + p.stderr)
        self.assertIn("<redacted>", p.stdout)
        self.assertIn('"company"."pm_conversations"', p.stdout)


@unittest.skipUnless(shutil.which("docker") and shutil.which("psql"), "docker + psql required")
class ScratchPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name = "provision-roles-test-" + uuid.uuid4().hex[:8]
        out = subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", cls.name, "-p", "127.0.0.1::5432",
             "-e", "POSTGRES_PASSWORD=scratchpw", "-e", "POSTGRES_DB=homely_company",
             "postgres:16-alpine"], capture_output=True, text=True)
        if out.returncode != 0:
            raise unittest.SkipTest("cannot start scratch postgres: " + out.stderr[:200])
        port = subprocess.run(["docker", "port", cls.name, "5432/tcp"], capture_output=True,
                              text=True).stdout.split(":")[-1].strip()
        cls.env = clean_env(PGDATABASE="homely_company", PGHOST="127.0.0.1", PGPORT=port, PGPASSWORD="scratchpw", **PASSWORDS)
        for _ in range(60):  # image restarts once during init; require stable readiness
            r = subprocess.run(["psql", "-X", "-U", "postgres", "-d", "homely_company", "-tAc", "select 1"],
                               env=cls.env, capture_output=True, text=True)
            if r.returncode == 0:
                time.sleep(2)
                r = subprocess.run(["psql", "-X", "-U", "postgres", "-d", "homely_company", "-tAc", "select 1"],
                                   env=cls.env, capture_output=True, text=True)
                if r.returncode == 0:
                    break
            time.sleep(1)
        else:
            cls.tearDownClass()
            raise unittest.SkipTest("scratch postgres never became ready")
        for f in ("company_schema.sql", "observer_schema.sql", "pm_schema.sql"):
            r = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
                                "-d", "homely_company", "-f", os.path.join(REPO, "sql", f)],
                               env=cls.env, capture_output=True, text=True)
            assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)

    def sql(self, query, user="postgres", password=None):
        env = dict(self.env)
        env["PGUSER"] = user
        if password:
            env["PGPASSWORD"] = password
        r = subprocess.run(["psql", "-X", "-tA", "-c", query], env=env, capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr

    def test_provision_idempotent_and_grants(self):
        for _ in range(2):  # second run must succeed unchanged
            p = run([], self.env)
            self.assertEqual(p.returncode, 0, p.stderr)
            for v in PASSWORDS.values():
                self.assertNotIn(v, p.stdout + p.stderr)
        for r in ROLES:
            rc, out, _ = self.sql("select current_user", r, PASSWORDS[
                {"hermes_company": "COMPANY_PASSWORD", "hermes_observer_writer": "OBSERVER_PASSWORD",
                 "hermes_analytics": "ANALYTICS_PASSWORD", "pm_agent_writer": "PM_AGENT_WRITER_PASSWORD"}[r]])
            self.assertEqual((rc, out), (0, r))
        pm_pw = PASSWORDS["PM_AGENT_WRITER_PASSWORD"]
        self.assertEqual(self.sql("select count(*) from company.pm_conversations", "pm_agent_writer", pm_pw)[0], 0)
        self.assertNotEqual(self.sql("select count(*) from company.plans", "pm_agent_writer", pm_pw)[0], 0)
        self.assertNotEqual(self.sql("delete from company.pm_conversations", "pm_agent_writer", pm_pw)[0], 0)
        obs = PASSWORDS["OBSERVER_PASSWORD"]
        self.assertNotEqual(self.sql("delete from observer.decisions", "hermes_observer_writer", obs)[0], 0)

    def test_only_role_does_not_disturb_others(self):
        self.assertEqual(run([], self.env).returncode, 0)
        env = dict(self.env)
        env["PM_AGENT_WRITER_PASSWORD"] = "rotated-0123456789"
        self.assertEqual(run(["--only", "pm_agent_writer"], env).returncode, 0)
        self.assertEqual(self.sql("select current_user", "pm_agent_writer", "rotated-0123456789")[1], "pm_agent_writer")
        self.assertEqual(self.sql("select 1", "hermes_company", PASSWORDS["COMPANY_PASSWORD"])[0], 0)

    def test_blank_password_does_not_clear_live_role(self):
        self.assertEqual(run([], self.env).returncode, 0)
        env = dict(self.env)
        env["PM_AGENT_WRITER_PASSWORD"] = ""
        self.assertEqual(run([], env).returncode, 2)
        self.assertEqual(self.sql("select 1", "pm_agent_writer", PASSWORDS["PM_AGENT_WRITER_PASSWORD"])[0], 0)

    def test_dsn_mismatch_detected(self):
        env = dict(self.env)
        env["PM_DATABASE_URL"] = "postgresql://pm_agent_writer:@postgres:5432/homely_company"
        p = run(["--only", "pm_agent_writer"], env)
        self.assertEqual(p.returncode, 3)
        self.assertIn("DSN MISMATCH", p.stderr)


if __name__ == "__main__":
    unittest.main()

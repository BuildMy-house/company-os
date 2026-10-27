"""hindsight-ensure-bank.py: creates a missing bank, no-op when present,
retries until healthy, never leaks the key; seed-hindsight.sh wiring.

Uses a local http.server fake Hindsight; the script runs via subprocess so the
CLI, env handling and exit codes are covered end to end.
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "hindsight-ensure-bank.py"
SEED = REPO / "scripts" / "seed-hindsight.sh"
KEY = "test-key-abcdef123456"


class FakeHindsight:
    def __init__(self, fail_health=0, existing_banks=()):
        self.fail_health = fail_health
        self.banks = set(existing_banks)
        self.gets, self.puts, self.auth_seen = [], [], []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, body=b"{}"):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                with outer.lock:
                    if self.path == "/health":
                        if outer.fail_health > 0:
                            outer.fail_health -= 1
                            return self._send(503)
                        return self._send(200)
                    bank = self.path.removeprefix("/v1/default/banks/")
                    outer.gets.append(bank)
                    outer.auth_seen.append(self.headers.get("Authorization"))
                    if bank in outer.banks:
                        return self._send(200, json.dumps({"id": bank}).encode())
                    return self._send(404, b'{"detail":"Bank not found"}')

            def do_PUT(self):  # noqa: N802
                bank = self.path.removeprefix("/v1/default/banks/")
                with outer.lock:
                    outer.puts.append((bank, self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()))
                    outer.auth_seen.append(self.headers.get("Authorization"))
                    outer.banks.add(bank)
                self._send(200)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def stop(self):
        self.httpd.shutdown()


def _run_script(fake, *extra, env_extra=None):
    env = dict(os.environ, HINDSIGHT_URL=fake.url, HINDSIGHT_API_KEY=KEY)
    env.pop("HERMES_HOME", None)
    env.update(env_extra or {})
    return subprocess.run(["python3", str(SCRIPT), "--wait", "10", *extra],
                          env=env, capture_output=True, text=True, timeout=120)


def _assert_no_leak(fake, *runs):
    for r in runs:
        assert KEY not in r.stdout + r.stderr
    assert all(a == f"Bearer {KEY}" for a in fake.auth_seen)


def test_creates_missing_bank_then_is_noop():
    fake = FakeHindsight()
    try:
        r1 = _run_script(fake, "--bank", "ceo")
        assert r1.returncode == 0, r1.stderr
        assert "created" in r1.stdout
        assert fake.puts == [("ceo", "{}")]
        r2 = _run_script(fake, "--bank", "ceo")
        assert r2.returncode == 0, r2.stderr
        assert "exists" in r2.stdout
        assert len(fake.puts) == 1  # second run: no PUT
    finally:
        fake.stop()
    _assert_no_leak(fake, r1, r2)


def test_noop_when_bank_already_present():
    fake = FakeHindsight(existing_banks=("pm-lead",))
    try:
        r = _run_script(fake, "--bank", "pm-lead")
        assert r.returncode == 0, r.stderr
        assert "exists" in r.stdout
        assert fake.puts == []
    finally:
        fake.stop()
    _assert_no_leak(fake, r)


def test_retries_until_healthy():
    fake = FakeHindsight(fail_health=2)
    try:
        r = _run_script(fake, "--bank", "ceo")
        assert r.returncode == 0, r.stderr
        assert len(fake.puts) == 1
    finally:
        fake.stop()
    _assert_no_leak(fake, r)


def test_unhealthy_within_wait_fails_without_put():
    fake = FakeHindsight(fail_health=10**9)
    try:
        r = _run_script(fake, "--bank", "ceo", "--wait", "3")
        assert r.returncode != 0
        assert fake.puts == []
        assert "not healthy" in r.stdout
    finally:
        fake.stop()
    _assert_no_leak(fake, r)


def test_default_bank_from_hermes_home_config(tmp_path):
    home = tmp_path / "data"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text(json.dumps({"bank_id": "custom-bank"}))
    fake = FakeHindsight()
    try:
        r = _run_script(fake, env_extra={"HERMES_HOME": str(home)})
        assert r.returncode == 0, r.stderr
        assert fake.puts == [("custom-bank", "{}")]
    finally:
        fake.stop()
    _assert_no_leak(fake, r)


def test_default_bank_is_ceo_without_config(tmp_path):
    fake = FakeHindsight()
    try:
        r = _run_script(fake, env_extra={"HERMES_HOME": str(tmp_path / "missing")})
        assert r.returncode == 0, r.stderr
        assert fake.puts == [("ceo", "{}")]
    finally:
        fake.stop()
    _assert_no_leak(fake, r)


def test_missing_key_fails_without_any_request():
    fake = FakeHindsight()
    try:
        env = dict(os.environ, HINDSIGHT_URL=fake.url)
        env.pop("HINDSIGHT_API_KEY", None)
        r = subprocess.run(["python3", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode != 0
        assert "HINDSIGHT_API_KEY" in r.stdout
        assert fake.gets == [] and fake.puts == []
    finally:
        fake.stop()


def _run_seed(tmp_path, fake, env_extra=None):
    home = tmp_path / "data"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HERMES_HOME=str(home), HINDSIGHT_SRC=str(REPO / "hermes" / "hindsight"),
               HINDSIGHT_URL=fake.url, HINDSIGHT_API_KEY=KEY)
    env.update(env_extra or {})
    r = subprocess.run(["bash", str(SEED)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r, home


def test_seed_launches_ensure_bank_in_background(tmp_path):
    fake = FakeHindsight()
    try:
        r, home = _run_seed(tmp_path, fake)
        assert (home / "hindsight" / "config.json").exists()
        deadline = time.monotonic() + 15
        while not fake.puts and time.monotonic() < deadline:
            time.sleep(0.2)
        assert fake.puts == [("ceo", "{}")]  # config.json bank_id, created in background
        assert KEY not in r.stdout + r.stderr
    finally:
        fake.stop()


def test_seed_skips_ensure_without_key_or_when_disabled(tmp_path):
    for name, env_extra in (("nokey", {"HINDSIGHT_API_KEY": ""}), ("disabled", {"HINDSIGHT_ENSURE_BANK": "0"})):
        fake = FakeHindsight()
        try:
            _run_seed(tmp_path / name, fake, env_extra)
            time.sleep(1)
            assert fake.gets == [] and fake.puts == []
        finally:
            fake.stop()

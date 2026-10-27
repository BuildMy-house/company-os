"""Hindsight memory wiring: seed idempotency, config safety, image pin, manifest."""
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "scripts" / "seed-hindsight.sh"
CONFIG = REPO / "hermes" / "hindsight" / "config.json"
MANIFEST = (REPO / "k8s" / "base" / "hindsight" / "hindsight.yaml").read_text()


def _run(tmp_path):
    home = tmp_path / "data"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HERMES_HOME=str(home), HINDSIGHT_SRC=str(CONFIG.parent), HINDSIGHT_ENSURE_BANK="0")
    return subprocess.run(["bash", str(SEED)], env=env, check=True, capture_output=True, text=True), home


def test_seed_creates_config_once_and_never_clobbers_edits(tmp_path):
    r, home = _run(tmp_path)
    dest = home / "hindsight" / "config.json"
    assert "seeded" in r.stdout
    assert json.loads(dest.read_text()) == json.loads(CONFIG.read_text())
    dest.write_text('{"mode": "local_external", "auto_retain": true}')
    r2, _ = _run(tmp_path)
    assert "leaving as is" in r2.stdout
    assert json.loads(dest.read_text())["auto_retain"] is True


def test_seed_without_source_is_noop(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    env = dict(os.environ, HERMES_HOME=str(home), HINDSIGHT_SRC=str(tmp_path / "nope"), HINDSIGHT_ENSURE_BANK="0")
    subprocess.run(["bash", str(SEED)], env=env, check=True, capture_output=True)
    assert not (home / "hindsight" / "config.json").exists()


def test_config_is_explicit_capture_and_secret_free():
    text = CONFIG.read_text()
    cfg = json.loads(text)
    assert cfg["mode"] == "local_external"
    assert cfg["bank_id"] == "ceo"
    assert cfg["memory_mode"] == "tools"
    assert cfg["auto_retain"] is False and cfg["auto_recall"] is False
    assert not [k for k in cfg if re.search(r"key|secret|password|^token$", k, re.I)]


def test_hermes_config_selects_hindsight_and_keeps_builtin():
    y = (REPO / "hermes" / "config.yaml").read_text()
    assert re.search(r"^\s+provider:\s*hindsight\s*$", y, re.M)
    assert re.search(r"memory_enabled:\s*true", y)
    assert re.search(r"user_profile_enabled:\s*true", y)


def test_entrypoint_seeds_and_forwards_key():
    e = (REPO / "scripts" / "entrypoint.sh").read_text()
    assert "seed-hindsight.sh" in e
    assert "HINDSIGHT_API_KEY" in e


def test_dockerfile_pins_plugin_and_client():
    d = (REPO / "Dockerfile").read_text()
    assert re.search(r"HINDSIGHT_PLUGIN_SHA=[0-9a-f]{40}", d)
    assert "hindsight-client==0.10.3" in d


def test_manifest_nous_tags_and_secret_refs_only():
    assert "https://inference-api.nousresearch.com/v1" in MANIFEST
    assert '"tags":["product=hermes-agent","user=hermes-agent"]' in MANIFEST
    for key in ("NOUS_API_KEY", "HINDSIGHT_API_KEY"):
        assert re.search(rf"secretKeyRef:\s*\n\s+name: company-ops-secrets\s*\n\s+key: {key}", MANIFEST)
    assert not re.search(r"(API_KEY|TOKEN)\s*\n\s+value:", MANIFEST)
    assert "@sha256:" in MANIFEST
    assert "api.z.ai" not in MANIFEST and "tokenrouter" not in MANIFEST.lower()


def test_manifest_has_pvc_probes_limits_and_overlay_wiring():
    for needle in ("PersistentVolumeClaim", "startupProbe", "livenessProbe", "readinessProbe",
                   "limits:", "requests:", "type: Recreate", "kind: Service"):
        assert needle in MANIFEST
    overlay = (REPO / "k8s" / "overlays" / "buildmy-prod" / "kustomization.yaml").read_text()
    assert "../../base/hindsight" in overlay


def test_seed_migrates_only_the_old_default_bank(tmp_path):
    home = tmp_path / "data"
    (home / "hindsight").mkdir(parents=True)
    dest = home / "hindsight" / "config.json"
    dest.write_text(json.dumps({"mode": "local_external", "bank_id": "hermes", "retain_tags": ["hermes", "explicit"], "auto_retain": True}))
    r, _ = _run(tmp_path)
    cfg = json.loads(dest.read_text())
    assert "migrated bank_id hermes -> ceo" in r.stdout
    assert cfg["bank_id"] == "ceo" and cfg["retain_tags"] == ["ceo", "explicit"]
    assert cfg["auto_retain"] is True  # operator edits survive
    dest.write_text(json.dumps({"bank_id": "custom"}))
    _run(tmp_path)
    assert json.loads(dest.read_text())["bank_id"] == "custom"


def test_copy_bank_script_pages_and_is_idempotent_by_document_id():
    import importlib.util
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    spec = importlib.util.spec_from_file_location("copybank", REPO / "scripts" / "hindsight-copy-bank.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    posted = []
    rows = [{"id": f"m{i}", "text": f"fact {i}", "tags": ["x"]} for i in range(3)]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            assert "/banks/hermes/memories/list" in self.path
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"items": rows}).encode())

        def do_POST(self):  # noqa: N802
            posted.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        n = mod.copy_bank(f"http://127.0.0.1:{httpd.server_port}", "k", "hermes", "ceo")
    finally:
        httpd.shutdown()
    assert n == 3
    path, body = posted[0]
    assert path == "/v1/default/banks/ceo/memories"
    assert [i["document_id"] for i in body["items"]] == ["copy-hermes-m0", "copy-hermes-m1", "copy-hermes-m2"]

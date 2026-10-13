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
    env = dict(os.environ, HERMES_HOME=str(home), HINDSIGHT_SRC=str(CONFIG.parent))
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
    env = dict(os.environ, HERMES_HOME=str(home), HINDSIGHT_SRC=str(tmp_path / "nope"))
    subprocess.run(["bash", str(SEED)], env=env, check=True, capture_output=True)
    assert not (home / "hindsight" / "config.json").exists()


def test_config_is_explicit_capture_and_secret_free():
    text = CONFIG.read_text()
    cfg = json.loads(text)
    assert cfg["mode"] == "local_external"
    assert cfg["bank_id"] == "hermes"
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

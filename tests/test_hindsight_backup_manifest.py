"""Hindsight PVC backup CronJob: RWO-safe, read-only, wired into the base."""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
BACKUPS = REPO / "k8s" / "base" / "backups"
JOB = yaml.safe_load((BACKUPS / "hindsight-data-backup.yaml").read_text())
POD = JOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]


def test_listed_in_backups_kustomization():
    k = yaml.safe_load((BACKUPS / "kustomization.yaml").read_text())
    assert "hindsight-data-backup.yaml" in k["resources"]


def test_mounts_hindsight_pvc_read_only():
    vol = POD["volumes"][0]["persistentVolumeClaim"]
    assert vol == {"claimName": "hindsight-data", "readOnly": True}
    assert all(m["readOnly"] for m in POD["containers"][0]["volumeMounts"])


def test_colocated_with_hindsight_pod_for_rwo():
    terms = POD["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert terms[0]["topologyKey"] == "kubernetes.io/hostname"
    expr = terms[0]["labelSelector"]["matchExpressions"][0]
    assert expr["key"] == "app" and expr["values"] == ["hindsight"]


def test_runs_hindsight_mode_with_r2_secret_and_no_overlap():
    c = POD["containers"][0]
    assert c["command"][-2:] == ["--hindsight-data", "/mnt/hindsight-data"]
    assert c["envFrom"] == [{"secretRef": {"name": "company-ops-secrets"}}]
    assert JOB["spec"]["concurrencyPolicy"] == "Forbid"
    assert POD["securityContext"]["runAsUser"] == 1000

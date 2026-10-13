"""Smoke test for scripts/new-project-overlay.sh (COMP-10).

Scaffolds a scratch overlay inside the repo working tree, checks the four
token replacements and the guard rails, then deletes it. The scratch name is
unique per process so concurrent test runs do not collide; the script itself
refuses to overwrite, which is part of what is asserted.
"""

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "new-project-overlay.sh"
NAME = f"comp10-scratch-{os.getpid()}"
DST = REPO / "k8s" / "overlays" / NAME


def run(*args):
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO
    )


def test_scaffold_replaces_tokens_and_guards():
    try:
        r = run(NAME, "comp10-ns", "registry.scratch.internal:5000")
        assert r.returncode == 0, r.stderr + r.stdout

        k = (DST / "kustomization.yaml").read_text()
        assert "acme" not in k and "Acme" not in k
        assert "namespace: comp10-ns" in k
        assert f"value: {NAME}-secrets" in k
        assert (
            f"newName: registry.scratch.internal:5000/{NAME}-hive" in k
        )
        assert (
            f"newName: registry.scratch.internal:5000/{NAME}-pm-agent" in k
        )
        assert f'HIVE_NAME, value: "{NAME.capitalize()} Hive"' in k

        ns = (DST / "namespace.yaml").read_text()
        assert "name: comp10-ns" in ns and "acme" not in ns

        # refuse overwrite
        r2 = run(NAME, "other-ns", "reg")
        assert r2.returncode != 0
        assert "refusing to overwrite" in r2.stderr

        # invalid project name
        r3 = run("Bad_Name", "ns", "reg")
        assert r3.returncode != 0
        assert "must match" in r3.stderr

        # wrong arg count
        r4 = run(NAME)
        assert r4.returncode != 0
        assert "usage:" in r4.stderr
    finally:
        if DST.exists():
            shutil.rmtree(DST)


def test_scratch_dir_removed():
    assert not DST.exists()

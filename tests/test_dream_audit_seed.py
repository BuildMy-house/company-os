"""dream-audit seeding: idempotency, agent-skill preservation, schedule/TZ, pre-run script."""
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEED = REPO / "scripts" / "seed-dream-audit.sh"
ENTRYPOINT = (REPO / "scripts" / "entrypoint.sh").read_text()

FAKE_HERMES = """#!/usr/bin/env bash
# fake hermes: `cron create` appends to $HERMES_HOME/cron/jobs.json, `cron list` prints names
H="${HERMES_HOME}"; mkdir -p "$H/cron"; J="$H/cron/jobs.json"; [ -f "$J" ] || echo '[]' > "$J"
echo "$@" >> "$H/hermes-calls.log"
if [ "$1 $2" = "cron create" ]; then
  python3 - "$J" "$@" <<'PY'
import json, sys
j, args = sys.argv[1], sys.argv[2:]
jobs = json.load(open(j)); jobs.append({"name": args[args.index("--name")+1], "argv": args})
json.dump(jobs, open(j, "w"))
PY
elif [ "$1 $2" = "cron list" ]; then
  python3 -c "import json;[print(x['name']) for x in json.load(open('$J'))]"
fi
"""


def _env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    h = bindir / "hermes"
    h.write_text(FAKE_HERMES)
    h.chmod(0o755)
    home = tmp_path / "data"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", HERMES_HOME=str(home),
               DREAM_SRC=str(REPO / "hermes"))
    return env, home


def _seed(env):
    subprocess.run(["bash", str(SEED)], env=env, check=True, capture_output=True)


def test_seed_is_idempotent_and_creates_one_job(tmp_path):
    env, home = _env(tmp_path)
    _seed(env)
    _seed(env)
    jobs = json.loads((home / "cron" / "jobs.json").read_text())
    assert [j["name"] for j in jobs] == ["dream-audit"]
    argv = jobs[0]["argv"]
    assert argv[2] == "0 0,12 * * *"
    assert argv[argv.index("--skill") + 1] == "dream-audit"
    assert argv[argv.index("--provider") + 1] == "custom:nous"
    assert argv[argv.index("--model") + 1] == "meituan/longcat-2.5-preview:free"
    assert argv[argv.index("--deliver") + 1] == "discord:#human-in-the-loop"
    assert (home / "dream-audit.mode").read_text().strip() == "audit"


def test_mode_flag_and_agent_skills_not_clobbered(tmp_path):
    env, home = _env(tmp_path)
    agent_skill = home / "skills" / "agent-made" / "SKILL.md"
    agent_skill.parent.mkdir(parents=True)
    agent_skill.write_text("mine")
    stale = home / "skills" / "dream-audit" / "SKILL.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    (home / "dream-audit.mode").write_text("apply\n")
    _seed(env)
    assert agent_skill.read_text() == "mine"
    assert "HARD RULES" in stale.read_text()  # dream-audit refreshed from image
    assert (home / "dream-audit.mode").read_text().strip() == "apply"  # Board flag kept


def test_cron_expression_maps_bhutan_time_to_utc():
    # 06:00 and 18:00 BTT (UTC+6) -> 00:00 and 12:00 UTC
    hours = {(6 - 6) % 24, (18 - 6) % 24}
    assert hours == {0, 12}
    assert re.search(r'DREAM_CRON="\$\{DREAM_AUDIT_CRON:-0 0,12 \* \* \*\}"', SEED.read_text())
    assert re.search(r"^timezone: UTC$", (REPO / "hermes" / "config.yaml").read_text(), re.M)


def test_entrypoint_seeds_memories_only_when_missing_and_calls_seed():
    assert re.search(r'\[\[ ! -f "/opt/data/memories/\$memory_file" &&', ENTRYPOINT)
    assert "seed-dream-audit.sh" in ENTRYPOINT


def test_prepare_script_snapshots_and_defaults_to_audit(tmp_path):
    env, home = _env(tmp_path)
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("fact")
    (home / "memories" / "USER.md").write_text("user")
    _seed(env)
    out = subprocess.run(["bash", str(home / "scripts" / "dream-audit-prepare.sh")],
                         env=env, check=True, capture_output=True, text=True).stdout
    ctx = json.loads(out.strip().splitlines()[-1])
    assert ctx["wakeAgent"] is True and ctx["context"]["mode"] == "audit"
    assert (Path(ctx["context"]["run_dir"]) / "pre-run" / "MEMORY.md").read_text() == "fact"

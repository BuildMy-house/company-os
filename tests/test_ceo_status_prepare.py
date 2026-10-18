"""ceo-status-prepare.sh: first/second run, truncation, jobs.json shapes, JSON escaping."""
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "hermes" / "scripts" / "ceo-status-prepare.sh"


def _run(home):
    env = dict(os.environ, HERMES_HOME=str(home))
    out = subprocess.run(["bash", str(SCRIPT)], env=env, check=True,
                         capture_output=True, text=True).stdout
    lines = out.strip().splitlines()
    return json.loads(lines[-1]), lines


def _write_output(home, job_id, text, mtime=None):
    d = home / "cron" / "output" / job_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / "20260101T000000Z.md"
    f.write_text(text)
    if mtime:
        os.utime(f, (mtime, mtime))


def _write_jobs(home, shape):
    job = {"name": "ceo-status-update", "id": "abc123"}
    data = [job] if shape == "list" else {"jobs": [job]}
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps(data))


def test_first_run_no_output(tmp_path):
    ctx, _ = _run(tmp_path)
    assert ctx["wakeAgent"] is True
    assert ctx["context"]["first_run"] is True
    assert ctx["context"]["previous_report"] == ""
    since = datetime.fromisoformat(ctx["context"]["since"].replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - since
    assert timedelta(hours=1) < age < timedelta(hours=3)
    # run time recorded for the next run
    datetime.fromisoformat((tmp_path / "ceo-status.last-run").read_text().strip().replace("Z", "+00:00"))


def test_second_run_reads_newest_output(tmp_path):
    _write_jobs(tmp_path, "list")
    old, new = 1.0, 2.0
    _write_output(tmp_path, "abc123", "older report\n", mtime=old)
    _write_output(tmp_path, "abc123", "newer report\n", mtime=new)
    ctx, _ = _run(tmp_path)
    assert ctx["context"]["first_run"] is False
    # bash $(...) strips trailing newlines; harmless for next-run diffing
    assert ctx["context"]["previous_report"] == "newer report"


def test_truncates_to_last_6000_chars(tmp_path):
    _write_jobs(tmp_path, "list")
    _write_output(tmp_path, "abc123", "x" * 7000 + "tail-marker")
    ctx, _ = _run(tmp_path)
    report = ctx["context"]["previous_report"]
    assert len(report) == 6000
    assert report.endswith("tail-marker")


def test_jobs_json_object_shape(tmp_path):
    _write_jobs(tmp_path, "object")
    _write_output(tmp_path, "abc123", "object-shape report")
    ctx, _ = _run(tmp_path)
    assert ctx["context"]["first_run"] is False
    assert ctx["context"]["previous_report"] == "object-shape report"


def test_output_dir_keyed_by_name_when_id_absent(tmp_path):
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps([{"name": "ceo-status-update", "job_id": "abc123"}]))
    _write_output(tmp_path, "ceo-status-update", "name-keyed report")
    ctx, _ = _run(tmp_path)
    assert ctx["context"]["previous_report"] == "name-keyed report"


def test_missing_jobs_json_degrades_to_first_run(tmp_path):
    ctx, _ = _run(tmp_path)
    assert ctx["context"]["first_run"] is True
    assert ctx["context"]["previous_report"] == ""


def test_json_valid_with_quotes_and_newlines(tmp_path):
    _write_jobs(tmp_path, "list")
    report = '## New\n- "quoted" item\n\ttabbed\nback\\slash\nItems: a=open, "b"=changed\n'
    _write_output(tmp_path, "abc123", report)
    ctx, lines = _run(tmp_path)
    assert ctx["context"]["previous_report"] == report.rstrip("\n")
    assert len(lines) == 1 and "\n" not in lines[0]  # single-line JSON on stdout
    assert lines[0] == json.dumps(ctx, ensure_ascii=False)

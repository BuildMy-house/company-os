#!/usr/bin/env bash
# Pre-run script for the ceo-status-update cron job (runs outside the agent, so
# it can do the file work Hermes cannot: agent.disabled_toolsets includes
# file/terminal).
#  1. finds the job id by name in $HERMES_HOME/cron/jobs.json
#  2. derives `since` from the mtime of the newest saved report (only a run
#     that produced output advances the window; a failed run does not move it)
#  3. reads the newest saved report for that job
#  4. prints the final {"wakeAgent": true, "context": {...}} line.
# Never fails the cron because of missing files: degrades to first_run=true.
set -euo pipefail
H="${HERMES_HOME:-/opt/data}"
JOB_NAME="${JOB_NAME:-ceo-status-update}"
mkdir -p "$H"

JOB_ID="$(python3 - "$H/cron/jobs.json" "$JOB_NAME" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
jobs = data.get("jobs", data) if isinstance(data, dict) else data
for j in jobs or []:
    if isinstance(j, dict) and j.get("name") == sys.argv[2]:
        print(j.get("id") or j.get("job_id") or j.get("name"))
        break
PY
)"

SINCE="$(python3 - "$H" "${JOB_ID:-}" "$JOB_NAME" <<'PY'
import glob, os, sys, time
h, jid, name = sys.argv[1], sys.argv[2], sys.argv[3]
cands = []
for d in {jid, name}:
    if d:
        cands += glob.glob(os.path.join(h, "cron", "output", d, "*.md"))
mtime = max(map(os.path.getmtime, cands)) if cands else time.time() - 2 * 3600
print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)))
PY
)"

PREV_REPORT="$(python3 - "$H" "${JOB_ID:-}" "$JOB_NAME" <<'PY'
import glob, os, sys
h, jid, name = sys.argv[1], sys.argv[2], sys.argv[3]
cands = []
for d in {jid, name}:
    if d:
        cands += glob.glob(os.path.join(h, "cron", "output", d, "*.md"))
if not cands:
    sys.exit(0)
newest = max(cands, key=os.path.getmtime)
with open(newest, encoding="utf-8", errors="replace") as f:
    sys.stdout.write(f.read()[-6000:])
PY
)"

FIRST_RUN=false
[ -n "$PREV_REPORT" ] || FIRST_RUN=true

python3 - "$SINCE" "$FIRST_RUN" "$PREV_REPORT" <<'PY'
import json, sys
since, first_run, report = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({"wakeAgent": True,
                  "context": {"since": since,
                              "first_run": first_run == "true",
                              "previous_report": report}},
                 ensure_ascii=False))
PY

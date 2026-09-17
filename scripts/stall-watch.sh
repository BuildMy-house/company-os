#!/usr/bin/env bash
# Companion stall-detector for a backgrounded model dispatch (opencode/codex/agy/etc).
#
# Process liveness alone is not enough to catch a hung dispatch — a model can
# sit alive for hours producing zero output (observed: two `codex exec`
# processes frozen at "Reading additional input from stdin..." for 4.5+ hours
# before anyone noticed, because the process itself never died). This watches
# the dispatch's own output/log file for actual growth, not just whether the
# PID still exists.
#
# Usage: stall-watch.sh <logfile> <pid> [stall_secs=600] [hard_cap_secs=1800] [label=dispatch]
#
# Run this via Bash with run_in_background:true immediately after launching
# any backgrounded model dispatch. It exits (and so fires a single
# notification) in exactly one of three ways:
#   - the dispatch process exits on its own -> exit 0, no stall (the normal
#     completion notification for the dispatch itself already covers this)
#   - the log file hasn't grown in `stall_secs` (default 10 min) while the
#     process is still alive -> exit 1, STALL DETECTED, kill + redispatch
#   - `hard_cap_secs` (default 30 min) elapses regardless of trickle output
#     -> exit 2, HARD CAP HIT, needs a manual look even if not fully frozen
#
# Note for ai-cli-launched dispatches specifically: ai-cli's own per-dispatch
# output file only writes an initial "started" payload and then goes silent
# until full completion — it does NOT stream incremental progress the way a
# raw `opencode run`/`codex exec` foreground process's stdout does. Pointing
# this script at that file will misfire a stall on a perfectly healthy
# dispatch. For ai-cli dispatches, point LOGFILE at the shared
# ~/.local/share/opencode/log/opencode.log instead (coarser signal — global
# across sessions — so pair it with `ai-cli ps`/a PID liveness check and use
# a longer stall_secs, e.g. 900, since unrelated concurrent sessions can mask
# a genuinely stalled one for a bit).
LOGFILE="$1"; PID="$2"; STALL_SECS="${3:-600}"; HARD_CAP="${4:-1800}"; LABEL="${5:-dispatch}"
START=$(date +%s)
LAST_SIZE=-1
LAST_CHANGE=$START
while true; do
  sleep 30
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "$LABEL: process $PID exited on its own — normal completion path handles this, no stall."
    exit 0
  fi
  NOW=$(date +%s)
  CUR_SIZE=$(stat -c%s "$LOGFILE" 2>/dev/null || echo 0)
  if [ "$CUR_SIZE" != "$LAST_SIZE" ]; then
    LAST_SIZE=$CUR_SIZE
    LAST_CHANGE=$NOW
  fi
  SINCE_CHANGE=$((NOW - LAST_CHANGE))
  TOTAL=$((NOW - START))
  if [ "$SINCE_CHANGE" -ge "$STALL_SECS" ]; then
    echo "STALL DETECTED: $LABEL (pid $PID) — $LOGFILE has not grown in ${SINCE_CHANGE}s (>= ${STALL_SECS}s threshold), process still alive. Recommend kill+redispatch."
    exit 1
  fi
  if [ "$TOTAL" -ge "$HARD_CAP" ]; then
    echo "HARD CAP HIT: $LABEL (pid $PID) — ${TOTAL}s elapsed (>= ${HARD_CAP}s cap) regardless of growth. Recommend review."
    exit 2
  fi
done

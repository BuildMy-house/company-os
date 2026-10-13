#!/usr/bin/env bash
# Pre-run script for the dream-audit cron job (runs outside the agent, so it can
# do the file work Hermes cannot: agent.disabled_toolsets includes file/terminal).
#  1. reads the mode flag (default audit)   2. snapshots MEMORY.md/USER.md
#  3. harvests the previous run's diary block into DREAMS.md
#  4. prints the final {"wakeAgent": true, "context": {...}} line.
set -euo pipefail
H="${HERMES_HOME:-/opt/data}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$H/cron/output/dream-audit/$TS"
STATE="$H/dream-audit.state"
MODE="$(tr -d '[:space:]' < "$H/dream-audit.mode" 2>/dev/null || true)"
[ "$MODE" = "apply" ] || MODE="audit"
SINCE="$(cat "$STATE" 2>/dev/null || date -u -d '12 hours ago' +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$RUN_DIR/pre-run"
ok=true
for f in MEMORY.md USER.md; do
  if [ -f "$H/memories/$f" ]; then cp -p "$H/memories/$f" "$RUN_DIR/pre-run/$f" || ok=false; fi
done

# Harvest the diary block from the previous run's saved output (newest first).
prev="$(ls -1t "$H"/cron/output/*/*.md 2>/dev/null | head -n 20 || true)"
for out in $prev; do
  if grep -q '<!-- DREAMS-ENTRY -->' "$out" 2>/dev/null; then
    if ! grep -qF "$(basename "$out")" "$H/.dreams-harvested" 2>/dev/null; then
      sed -n '/<!-- DREAMS-ENTRY -->/,$p' "$out" | sed '1d' >> "$H/DREAMS.md"
      echo >> "$H/DREAMS.md"
      basename "$out" >> "$H/.dreams-harvested"
    fi
    break
  fi
done

date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE"
mc=$(wc -c < "$H/memories/MEMORY.md" 2>/dev/null || echo 0)
uc=$(wc -c < "$H/memories/USER.md" 2>/dev/null || echo 0)
printf '{"wakeAgent": true, "context": {"mode": "%s", "run_dir": "%s", "since": "%s", "snapshot_ok": %s, "memory_chars": %s, "user_chars": %s}}\n' \
  "$MODE" "$RUN_DIR" "$SINCE" "$ok" "$mc" "$uc"

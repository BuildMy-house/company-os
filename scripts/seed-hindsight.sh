#!/usr/bin/env bash
# Seed the Hermes Hindsight plugin config, only if missing. Sourced/called by
# entrypoint.sh. Overridable for tests: HERMES_HOME, HINDSIGHT_SRC.
#
# Seed-if-missing (same rule as memories/*.md and dream-audit.mode): the file
# lives on the /opt/data PVC, so an operator edit (e.g. flipping auto_retain or
# adding a bank) survives restarts and image upgrades. To pick up a new image
# default, delete $HERMES_HOME/hindsight/config.json and restart.
#
# Credentials are never written here: the plugin reads HINDSIGHT_API_KEY from
# the environment / $HERMES_HOME/.env (entrypoint.sh forwards it).
set -uo pipefail

H="${HERMES_HOME:-/opt/data}"
SRC="${HINDSIGHT_SRC:-/opt/company-ops/hermes/hindsight}"
DEST="$H/hindsight/config.json"

if [ ! -f "$SRC/config.json" ]; then
  echo "[hindsight] WARN: $SRC/config.json missing; nothing to seed" >&2
  exit 0
fi

mkdir -p "$H/hindsight"
if [ -s "$DEST" ]; then
  echo "[hindsight] config already present; leaving as is"
  # One-time bank rename (docs/HINDSIGHT-BANKS.md): the original default bank
  # was "hermes"; the CEO's bank is now "ceo". Only that exact old default is
  # rewritten; any other operator value is left alone. Copy the old bank's
  # memories across first with scripts/hindsight-copy-bank.py.
  python3 - "$DEST" <<'PY' || echo "[hindsight] WARN: bank migration skipped" >&2
import json, sys
path = sys.argv[1]
cfg = json.load(open(path))
if cfg.get("bank_id") == "hermes":
    cfg["bank_id"] = "ceo"
    if cfg.get("retain_tags") == ["hermes", "explicit"]:
        cfg["retain_tags"] = ["ceo", "explicit"]
    if cfg.get("retain_source") == "hermes":
        cfg["retain_source"] = "ceo"
    json.dump(cfg, open(path, "w"), indent=2)
    print("[hindsight] migrated bank_id hermes -> ceo")
PY
else
  cp "$SRC/config.json" "$DEST"
  echo "[hindsight] seeded $DEST"
fi

# Bank creation (docs/HINDSIGHT-BANKS.md): Hindsight only creates a bank on a
# write, and the CEO is tools-only (auto_retain=false), so recall would 404 on
# a never-written bank. Ensure it in the background so boot never blocks; the
# script logs [hindsight]-prefixed lines and failure only warns. Skip with
# HINDSIGHT_ENSURE_BANK=0 (tests) or when no key is configured.
if [ -n "${HINDSIGHT_API_KEY:-}" ] && [ "${HINDSIGHT_ENSURE_BANK:-1}" != "0" ]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$HERE/hindsight-ensure-bank.py" ]; then
    python3 "$HERE/hindsight-ensure-bank.py" &
  else
    echo "[hindsight] WARN: $HERE/hindsight-ensure-bank.py missing; bank not ensured" >&2
  fi
fi
exit 0

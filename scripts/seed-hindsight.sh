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
else
  cp "$SRC/config.json" "$DEST"
  echo "[hindsight] seeded $DEST"
fi
exit 0

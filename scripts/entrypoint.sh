#!/usr/bin/env bash
set -euo pipefail

# Clone or pull homely-ceo at runtime
if [[ -d /opt/homely-ceo/.git ]]; then
  cd /opt/homely-ceo && git pull --ff-only 2>/dev/null || true
else
  git clone https://github.com/NaharEmet/homely-ceo.git /opt/homely-ceo 2>/dev/null || true
fi

if [[ "${1:-}" == "hermes" ]]; then
  shift
  exec hermes "$@"
fi

exec company-ops "$@"

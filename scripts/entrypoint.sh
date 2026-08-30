#!/usr/bin/env bash
set -euo pipefail

# Clone or pull hermes-operations at runtime
if [[ -d /opt/hermes-operations/.git ]]; then
  cd /opt/hermes-operations && git pull --ff-only 2>/dev/null || true
else
  git clone https://github.com/NaharEmet/hermes-operations.git /opt/hermes-operations 2>/dev/null || true
fi

if [[ "${1:-}" == "hermes" ]]; then
  shift
  exec hermes "$@"
fi

exec company-ops "$@"

#!/usr/bin/env bash
# pin-publisher-image.sh <image@sha256:digest>
# Replaces the placeholder image in the publisher Deployment (both containers) and the
# marketing-ingest CronJob with one digest-pinned publisher image ref. Edits files only;
# review the diff, commit it, then apply by hand (docs/PUBLISHER.md "Human steps").
set -euo pipefail
ref="${1:-}"
[[ "$ref" =~ ^[a-z0-9./:_-]+/company-os-publisher@sha256:[0-9a-f]{64}$ ]] \
  || { echo "usage: $0 <registry>/company-os-publisher@sha256:<64 hex>" >&2; exit 2; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sed -i -E "s#^(\s+image:) localhost:30500/company-os-publisher(:latest|@sha256:[0-9a-f]{64})\$#\1 ${ref}#" "$root/k8s/base/publisher/publisher.yaml"
sed -i -E "s#^(\s+image:) localhost:30500/company-os@sha256:[0-9a-f]{64}\$#\1 ${ref}#" "$root/k8s/base/marketing-ingest/marketing-ingest.yaml"
sed -i -E "s#^(\s+image:) .*/company-os-publisher@sha256:[0-9a-f]{64}\$#\1 ${ref}#" "$root/k8s/base/marketing-ingest/marketing-ingest.yaml" "$root/k8s/base/publisher/publisher.yaml"
n1=$(grep -c "image: ${ref}\$" "$root/k8s/base/publisher/publisher.yaml" || true)
n2=$(grep -c "image: ${ref}\$" "$root/k8s/base/marketing-ingest/marketing-ingest.yaml" || true)
[ "$n1" = 2 ] && [ "$n2" = 1 ] || { echo "expected 2 + 1 image lines pinned, got $n1 + $n2" >&2; exit 1; }
echo "pinned: publisher.yaml x$n1, marketing-ingest.yaml x$n2 -> $ref"

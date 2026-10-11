#!/usr/bin/env bash
set -euo pipefail

# Bring up the local k3s company-ops stack from any checkout path.
# - Secrets are provisioned by scripts/bootstrap-secrets.sh (see docs/SECRETS-INFISICAL.md) —
#   never from a local .env.
# - Images are built from this repo and pushed to the in-cluster registry
#   (localhost:30500) by scripts/build-push-images.sh — no locally pre-built
#   docker images needed. Set SKIP_BUILD=1 to reuse images already in the
#   registry.
# - k8s manifests are the source of truth for image refs; this script never
#   `kubectl set image`s a tag that is not in the manifests.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }

kubectl apply -f k8s/namespace.yaml

kubectl create configmap postgres-init \
  --namespace company-ops \
  --from-file=01-company_schema.sql=sql/company_schema.sql \
  --from-file=02-observer_schema.sql=sql/observer_schema.sql \
  --from-file=03-pm_schema.sql=sql/pm_schema.sql \
  --dry-run=client -o yaml | kubectl apply -f -

if [ -x scripts/bootstrap-secrets.sh ]; then
  scripts/bootstrap-secrets.sh
else
  echo "ERROR: scripts/bootstrap-secrets.sh not found or not executable." >&2
  echo "Secrets are provisioned from Infisical only; see docs/SECRETS-INFISICAL.md." >&2
  echo "This stack cannot start without its secrets." >&2
  exit 1
fi

kubectl apply -k k8s
kubectl rollout status deployment/registry -n company-ops --timeout=120s

if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "SKIP_BUILD=1: using images already present in the registry"
else
  command -v docker >/dev/null || { echo "docker is required for build-push-images.sh" >&2; exit 1; }
  scripts/build-push-images.sh
fi

# Re-roll every workload Deployment declared in the kustomization so it
# re-pulls its possibly-refreshed mutable-tag image. registry and postgres
# are excluded: registry serves the pulls (restarting it would race them)
# and postgres holds state. CronJobs (dns-healthcheck) pick up new images
# on their next scheduled run.
mapfile -t deps < <(
  for f in $(sed -n 's/^  - //p' k8s/kustomization.yaml); do
    awk '/^kind: Deployment/{d=1;next} d&&/^  name:/{print $2; d=0}' "k8s/$f"
  done | grep -v -e '^registry$' -e '^postgres$'
)

for dep in "${deps[@]}"; do
  kubectl rollout restart "deployment/$dep" -n company-ops
done
for dep in "${deps[@]}"; do
  kubectl rollout status "deployment/$dep" -n company-ops --timeout=180s
done

echo "NOTE: Postgres roles are no longer created by the init configmap. On a fresh"
echo "  database run scripts/provision-db-roles.sh (see NAHAR-TODO.md Group L)."

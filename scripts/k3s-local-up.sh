#!/usr/bin/env bash
set -euo pipefail

# Local k3s path. Secrets stay in the ignored .env; images stay local.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
test -f .env || { echo "company-os/.env is required" >&2; exit 1; }

kubectl apply -f k8s/namespace.yaml

kubectl create configmap postgres-init \
  --namespace company-ops \
  --from-file=01-company_schema.sql=sql/company_schema.sql \
  --from-file=02-observer_schema.sql=sql/observer_schema.sql \
  --from-file=03-roles.sql=sql/roles.sql \
  --from-file=04-set-role-passwords.sh=scripts/04-set-role-passwords.sh \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic company-ops-secrets \
  --namespace company-ops \
  --from-env-file=.env \
  --dry-run=client -o yaml | kubectl apply -f -

docker image inspect company-os-company-ops:latest >/dev/null
docker image inspect company-os-engineering:latest >/dev/null
docker save company-os-company-ops:latest | sudo k3s ctr images import -
docker save company-os-engineering:latest | sudo k3s ctr images import -

kubectl apply -k k8s

kubectl rollout restart deployment/company-ops deployment/engineering -n company-ops
kubectl rollout status deployment/company-ops -n company-ops --timeout=180s
kubectl rollout status deployment/engineering -n company-ops --timeout=180s

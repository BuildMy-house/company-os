#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

kubectl apply -f "$ROOT_DIR/k8s/rbac.yaml"
kubectl apply -f "$ROOT_DIR/k8s/registry.yaml"
kubectl rollout status deployment/registry -n company-ops --timeout=120s

docker tag docker.io/library/company-os:container-manager localhost:30500/company-os:container-manager
docker tag docker.io/library/company-os-engineering:container-manager localhost:30500/company-os-engineering:container-manager
docker push localhost:30500/company-os:container-manager
docker push localhost:30500/company-os-engineering:container-manager

kubectl apply -f "$ROOT_DIR/k8s/company-ops.yaml"
kubectl apply -f "$ROOT_DIR/k8s/engineering.yaml"
kubectl set image deployment/hermes-gateway hermes-gateway=localhost:30500/company-os:container-manager -n company-ops
kubectl set image deployment/engineering-agent engineering-agent=localhost:30500/company-os-engineering:container-manager -n company-ops
kubectl rollout status deployment/hermes-gateway -n company-ops --timeout=180s
kubectl rollout status deployment/engineering-agent -n company-ops --timeout=180s

echo "Local Company OS and engineering deployments are healthy."

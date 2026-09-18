#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="/tmp/company-os-images.tar"

docker save -o "$ARCHIVE" \
  docker.io/library/company-os:container-manager \
  docker.io/library/company-os-engineering:container-manager
sudo k3s ctr images import "$ARCHIVE"
rm -f "$ARCHIVE"

sudo kubectl apply -f "$ROOT_DIR/k8s/rbac.yaml"
sudo kubectl apply -f "$ROOT_DIR/k8s/company-ops.yaml"
sudo kubectl apply -f "$ROOT_DIR/k8s/engineering.yaml"
sudo kubectl rollout status deployment/hermes-gateway -n company-ops --timeout=180s
sudo kubectl rollout status deployment/engineering-agent -n company-ops --timeout=180s

echo "Local Company OS and engineering deployments are healthy."

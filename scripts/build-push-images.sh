#!/usr/bin/env bash
set -euo pipefail

# Reproducible build+push of every repo-owned image to a container registry
# (default: the in-cluster k3s registry at localhost:30500, k8s/registry.yaml).
# Works on a fresh host with only docker (buildx), git, and a reachable
# registry — no host-specific paths, no locally pre-built images.
#
# Usage: scripts/build-push-images.sh [--only NAME] [--dry-run] [--print-digests]
#   --only NAME       build one image: company-os|engineering|hive|pm-agent|browser-adversary
#   --dry-run         print the commands instead of running them
#   --print-digests   after the push, print image@sha256 refs for pinning manifests
#
# Env:
#   REGISTRY           target registry (default localhost:30500)
#   WORKSPACE_CONTEXT  git URL for Dockerfile.engineering's named "shared"
#                      build context (default the workspace repo URL; pin to
#                      URL#<40-hex-sha> for reproducible builds)
#   AGENT_FLAVOR       engineering build arg (default claude)
#   INSTALL_BROWSER    engineering build arg (default true)
#   GIT_AUTH_TOKEN     token for a private WORKSPACE_CONTEXT (e.g. a GitHub App
#                      installation token from scripts/github-app-token.js);
#                      passed to BuildKit as a build secret, never as an arg
#
# Tags: every image gets the immutable short-SHA tag of the current checkout,
# plus the mutable tag its k8s manifests reference (see the table below; keep
# in sync with k8s/*.yaml — see docs/IMAGES.md).

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

REGISTRY="${REGISTRY:-localhost:30500}"
WORKSPACE_CONTEXT="${WORKSPACE_CONTEXT:-https://github.com/BuildMy-house/workspace.git}"
AGENT_FLAVOR="${AGENT_FLAVOR:-claude}"
INSTALL_BROWSER="${INSTALL_BROWSER:-true}"

command -v docker >/dev/null || { echo "docker (with buildx) is required" >&2; exit 1; }
sha="$(git rev-parse --short HEAD)"

only="" dry_run=0 print_digests=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only) only="${2:?--only needs an image name}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    --print-digests) print_digests=1; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

run() {
  if [ "$dry_run" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi
}

builder=company-os-images
if [ "$dry_run" != 1 ] && ! docker buildx inspect "$builder" >/dev/null 2>&1; then
  # network=host so the builder container can reach a host-published registry
  # at localhost:30500 (otherwise "localhost" is the builder container itself).
  docker buildx create --name "$builder" --driver-opt network=host >/dev/null
fi

# name|image repo|dockerfile|mutable tag referenced by manifests|extra buildx args
images=(
  "company-os|company-os|Dockerfile|dns-remediation|"
  "engineering|company-os-engineering|Dockerfile.engineering|hive-bid-8b29112|--build-arg AGENT_FLAVOR=$AGENT_FLAVOR --build-arg INSTALL_BROWSER=$INSTALL_BROWSER --build-context shared=$WORKSPACE_CONTEXT"
  "hive|company-os-hive|hive/Dockerfile|events|"
  "pm-agent|company-os-pm-agent|pm-agent/Dockerfile|latest|"
  "browser-adversary|company-os-browser-adversary|Dockerfile.browser-adversary|latest|"
)

# BuildKit's git fetch runs inside the builder container, which has none of the
# client's credentials; a GIT_AUTH_TOKEN build secret is how it authenticates.
secret_args=()
if [ -n "${GIT_AUTH_TOKEN:-}" ]; then
  secret_args=(--secret id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN)
fi

built=()
for entry in "${images[@]}"; do
  IFS='|' read -r name repo dockerfile mutable extra <<<"$entry"
  if [ -n "$only" ] && [ "$name" != "$only" ]; then continue; fi
  image="$REGISTRY/$repo"
  echo "==> building $name -> $image:$sha (+ :$mutable)"
  # shellcheck disable=SC2086
  run docker buildx build --builder "$builder" --push \
    -f "$dockerfile" \
    -t "$image:$sha" \
    -t "$image:$mutable" \
    $extra \
    ${secret_args[@]+"${secret_args[@]}"} \
    .
  built+=("$repo")
done
if [ -n "$only" ] && [ ${#built[@]} -eq 0 ]; then
  echo "no image named '$only' (see the image table in docs/IMAGES.md)" >&2
  exit 2
fi

if [ "$print_digests" = 1 ]; then
  for repo in "${built[@]}"; do
    if [ "$dry_run" = 1 ]; then
      printf '+ docker buildx imagetools inspect %s/%s:%s --format "{{.Manifest.Digest}}"  # -> %s/%s@sha256:...\n' \
        "$REGISTRY" "$repo" "$sha" "$REGISTRY" "$repo"
    else
      digest="$(docker buildx imagetools inspect "$REGISTRY/$repo:$sha" --format '{{.Manifest.Digest}}')"
      printf '%s/%s@%s\n' "$REGISTRY" "$repo" "$digest"
    fi
  done
fi

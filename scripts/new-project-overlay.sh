#!/usr/bin/env bash
# new-project-overlay.sh <project> <namespace> <registry-host>
# Scaffolds k8s/overlays/<project> from k8s/overlays/example-standalone
# (hive + pm-agent for a different project, no fork). Stand-up guide:
# docs/STANDALONE-HIVE-PM.md.
set -euo pipefail

usage() {
  echo "usage: $0 <project> <namespace> <registry-host>" >&2
  echo "  project        [a-z0-9-]+ ; creates k8s/overlays/<project>" >&2
  echo "  namespace      [a-z0-9-]+ ; replaces 'acme-ops'" >&2
  echo "  registry-host  host[:port][/prefix] ; replaces 'registry.example.internal:5000'" >&2
  exit 1
}

[[ $# -eq 3 ]] || usage
project="$1"
namespace="$2"
registry_host="$3"

[[ "$project" =~ ^[a-z0-9-]+$ ]] || { echo "error: project must match [a-z0-9-]+, got '$project'" >&2; exit 1; }
[[ "$namespace" =~ ^[a-z0-9-]+$ ]] || { echo "error: namespace must match [a-z0-9-]+, got '$namespace'" >&2; exit 1; }
[[ -n "$registry_host" ]] || { echo "error: registry-host must not be empty" >&2; exit 1; }

repo_root="$(git rev-parse --show-toplevel)"
src="$repo_root/k8s/overlays/example-standalone"
dst="$repo_root/k8s/overlays/$project"

[[ -d "$src" ]] || { echo "error: template not found: $src" >&2; exit 1; }
[[ ! -e "$dst" ]] || { echo "error: refusing to overwrite existing $dst" >&2; exit 1; }

cp -r "$src" "$dst"

# Capitalised project name for "Acme Hive"-style strings in the template.
project_cf="$(tr '[:lower:]' '[:upper:]' <<<"${project:0:1}")${project:1}"

# Order matters: 'acme-ops'/'acme-secrets' before bare 'acme' so the longer
# tokens are not half-substituted.
find "$dst" -type f -exec sed -i \
  -e "s|acme-ops|$namespace|g" \
  -e "s|acme-secrets|${project}-secrets|g" \
  -e "s|Acme|$project_cf|g" \
  -e "s|acme|$project|g" \
  -e "s|registry.example.internal:5000|$registry_host|g" {} +

echo "scaffolded $dst"

# Gate the new overlay when a renderer is available; skip with a note otherwise.
if [[ -n "${KUSTOMIZE:-}" ]] || command -v kustomize >/dev/null 2>&1 || command -v kubectl >/dev/null 2>&1; then
  FORBID_LITERALS="buildmyhouse company-ops localhost:30500 homely" SKIP_BASELINE=1 \
    "$repo_root/scripts/verify-kustomize-render.sh" "k8s/overlays/$project"
else
  echo "note: no kustomize renderer found; run the gate later:"
  echo "  FORBID_LITERALS=\"buildmyhouse company-ops localhost:30500 homely\" SKIP_BASELINE=1 scripts/verify-kustomize-render.sh k8s/overlays/$project"
fi

cat <<EOF
next steps (full guide: docs/STANDALONE-HIVE-PM.md):
  1. edit k8s/overlays/$project/kustomization.yaml:
     replace 'replace-with-immutable-tag' with the immutable tags you pushed
     to $registry_host; adjust HIVE_NAME/HIVE_DESCRIPTION in the hive patch
  2. author pm-agent/content/$project/{SOUL.md,refusal.txt} and set
     PM_AGENT_CONTENT_DIR on the pm-agent container (overlay patch)
  3. create the Secrets out of band (never commit them): $project-secrets
     (hive envFrom; gateway PM_AGENT_AUTH_TOKEN) and pm-agent-secrets
     (PM_DATABASE_URL, PM_AGENT_LLM_API_KEY)
  4. render + gate:
     FORBID_LITERALS="buildmyhouse company-ops localhost:30500 homely" \\
     SKIP_BASELINE=1 scripts/verify-kustomize-render.sh k8s/overlays/$project
  5. hand the rendered manifest to whoever owns cluster access
     (apply through your normal deploy path)
EOF

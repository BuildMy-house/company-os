#!/usr/bin/env bash
set -euo pipefail

# bootstrap-secrets.sh — materialize every company-os k8s Secret from Infisical.
#
# The ONLY manually provisioned secret on a new host is the Infisical
# bootstrap machine identity (Universal Auth clientId/clientSecret), exported
# as INFISICAL_UNIVERSAL_AUTH_CLIENT_ID / INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET
# before running this script. Everything else is pulled from Infisical:
#
#   company-ops-secrets                        <- project root + /infra + /hermes (merged)
#   company-ops-steward-engineering-agent      <- /infra/steward/engineering-agent
#   company-ops-steward-engineering-opencode   <- /infra/steward/engineering-opencode
#   company-ops-steward-engineering-opencode-direct <- .../engineering-opencode-direct
#   buildmyhouse-dev-secrets (buildmyhouse-dev)<- buildmyhouse-app project, root
#   infisical-bootstrap-credentials            <- the exported env vars above
#
# builder-manager-token / buildmyhouse-dev-deployer-token are cluster-issued
# service-account-token Secrets; they are (re)created by applying the RBAC
# manifests below, not from Infisical (they are not portable across clusters).
#
# Secrets are applied statically here so workloads can start BEFORE the
# Infisical Secrets Operator is installed; if the operator CRDs are present,
# the live-sync manifests are applied too and take over from then on.
#
# Usage:
#   scripts/bootstrap-secrets.sh            # fetch + apply (needs kubectl/node/jq)
#   scripts/bootstrap-secrets.sh --dry-run  # print target Secrets and KEY NAMES
#                                           # only; no Infisical or cluster access
#
# Inventory + rotation notes: docs/SECRETS-INFISICAL.md

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

INFISICAL_HOST_URL="${INFISICAL_HOST_URL:-https://eu.infisical.com}"
INFISICAL_ENV="${INFISICAL_ENV:-dev}"
# "Build My house" project (company-os runtime secrets) and "buildmyhouse-app"
# project; same ids as k8s/infisical-sync.yaml / k8s/buildmyhouse-dev-infisical-sync.yaml.
OPS_PROJECT_ID="${INFISICAL_PROJECT_ID:-8806c2b0-73d2-4bea-8537-5b874c5ff592}"
APP_PROJECT_ID="${APP_PROJECT_ID:-58b43b81-effb-4392-a937-46f2448efb78}"
OPS_NS="company-ops"
APP_NS="buildmyhouse-dev"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

if [ "$DRY_RUN" = "1" ]; then
  echo "# bootstrap-secrets.sh --dry-run — plan only, KEY NAMES ONLY, no values."
  echo "# Infisical: ${INFISICAL_HOST_URL} env=${INFISICAL_ENV}"
  echo
  echo "infisical-bootstrap-credentials (${OPS_NS} + ${APP_NS})"
  echo "  clientId"
  echo "  clientSecret"
  echo
  echo "company-ops-secrets (${OPS_NS}) <- ${OPS_PROJECT_ID} [/, /infra, /hermes merged]"
  echo "  /          AXIOM_DASHBOARD_TOKEN AXIOM_ORG_ID AXIOM_TOKEN BUILDMYHOUSE_APP_URL BUILDMYHOUSE_MCP_BEARER_TOKEN BUILDMYHOUSE_MCP_URL BUILDMYHOUSE_TEST_EMAIL BUILDMYHOUSE_TEST_PASSWORD"
  echo "  /infra     API_SERVER_KEY AXIOM_ORG_ID AXIOM_TOKEN CLAUDE_CODE_OAUTH_TOKEN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_API_TOKEN GITHUB_APP_ID GITHUB_APP_INSTALLATION_ID GITHUB_APP_PRIVATE_KEY NEON_API_KEY OPENCODE_GO_API_KEY R2_ACCESS_KEY_ID R2_API_TOKEN R2_BUCKET R2_ENDPOINT R2_SECRET_ACCESS_KEY STEWARD_TOKEN STEWARD_URL TOKENROUTER_API_KEY VITE_AXIOM_TOKEN ZAI_CODING_PLAN_API_KEY"
  echo "  /hermes    ANALYTICS_DATABASE_URL ANALYTICS_PASSWORD COMPANY_DATABASE_URL COMPANY_PASSWORD DISCORD_ALLOW_ALL_USERS DISCORD_ALLOWED_CHANNELS DISCORD_ANNOUNCE_CHANNEL DISCORD_BLOG_CHANNEL DISCORD_BOT_TOKEN DISCORD_DM_USER DISCORD_FINANCE_CHANNEL DISCORD_HIL_CHANNEL NOUS_API_KEY OBSERVER_DATABASE_URL OBSERVER_PASSWORD POSTGRES_PASSWORD STEWARD_TOKEN"
  for s in engineering-agent engineering-opencode engineering-opencode-direct; do
    echo
    echo "company-ops-steward-${s} (${OPS_NS}) <- ${OPS_PROJECT_ID} /infra/steward/${s}"
    echo "  STEWARD_TOKEN"
  done
  echo
  echo "buildmyhouse-dev-secrets (${APP_NS}) <- ${APP_PROJECT_ID} /"
  echo "  ANALYTICS_ADMIN_TOKEN AXIOM_DATASET AXIOM_ENDPOINT AXIOM_TOKEN BUILDMYHOUSE_MCP_TOKEN CLOUDFLARE_API_TOKEN JWT_SECRET LUXCORE_WORKER_TOKEN NEON_API_KEY R2_ACCESS_KEY_ID R2_ACCOUNT_ID R2_BUCKET_NAME R2_PUBLIC_URL R2_S3_ENDPOINT R2_SECRET_ACCESS_KEY RESEND_API_KEY RESEND_FROM_EMAIL VITE_AXIOM_DATASET VITE_AXIOM_ENDPOINT VITE_AXIOM_TOKEN"
  echo
  echo "builder-manager-token (${OPS_NS}), buildmyhouse-dev-deployer-token (${OPS_NS})"
  echo "  cluster-issued SA tokens — recreated by kubectl apply of k8s/builder-rbac.yaml + k8s/buildmyhouse-dev-deployer-rbac.yaml"
  exit 0
fi

for var in INFISICAL_UNIVERSAL_AUTH_CLIENT_ID INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var is not set — this is the single manually provisioned bootstrap credential" >&2
    exit 1
  fi
done
for cmd in kubectl node jq; do
  command -v "$cmd" >/dev/null || { echo "ERROR: $cmd is required" >&2; exit 1; }
done

TMP="$(mktemp -d)"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT

fetch() { # fetch <projectId> <secretPath> <outfile>
  INFISICAL_HOST_URL="$INFISICAL_HOST_URL" \
  INFISICAL_PROJECT_ID="$1" \
  INFISICAL_ENV="$INFISICAL_ENV" \
  INFISICAL_SECRET_PATH="$2" \
  node "$SCRIPT_DIR/fetch-infisical-secrets.js" --json > "$3"
}

# JSON array of {secretKey,secretValue}(s) -> Secret manifest, applied idempotently.
apply_secret() { # apply_secret <name> <namespace> <file...>
  local name="$1" ns="$2"; shift 2
  jq -s 'add
         | map({(.secretKey): .secretValue}) | add
         | if length == 0 then error("no secrets fetched") else . end
         | {apiVersion: "v1", kind: "Secret",
            metadata: {name: $n, namespace: $ns},
            type: "Opaque",
            data: (to_entries | map({key: .key, value: (.value | @base64)}) | from_entries)}' \
    --arg n "$name" --arg ns "$ns" "$@" | kubectl apply -f -
  echo "bootstrapped Secret ${name} (namespace ${ns})"
}

echo "[1/6] Ensuring namespaces..."
kubectl apply -f "$REPO_ROOT/k8s/namespace.yaml" >/dev/null
kubectl apply -f "$REPO_ROOT/k8s/buildmyhouse-dev-namespace.yaml" >/dev/null

echo "[2/6] Seeding infisical-bootstrap-credentials (the only manually provisioned secret)..."
jq -n '{apiVersion: "v1", kind: "Secret",
        metadata: {name: "infisical-bootstrap-credentials", namespace: $ns},
        type: "Opaque",
        data: {clientId: ($cid | @base64), clientSecret: ($cs | @base64)}}' \
  --arg ns "$OPS_NS" \
  --arg cid "$INFISICAL_UNIVERSAL_AUTH_CLIENT_ID" \
  --arg cs "$INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET" | kubectl apply -f - >/dev/null
jq -n '{apiVersion: "v1", kind: "Secret",
        metadata: {name: "infisical-bootstrap-credentials", namespace: $ns},
        type: "Opaque",
        data: {clientId: ($cid | @base64), clientSecret: ($cs | @base64)}}' \
  --arg ns "$APP_NS" \
  --arg cid "$INFISICAL_UNIVERSAL_AUTH_CLIENT_ID" \
  --arg cs "$INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET" | kubectl apply -f - >/dev/null

echo "[3/6] Fetching ${OPS_PROJECT_ID} root + /infra + /hermes -> company-ops-secrets..."
fetch "$OPS_PROJECT_ID" /            "$TMP/ops-root.json"
fetch "$OPS_PROJECT_ID" /infra       "$TMP/ops-infra.json"
fetch "$OPS_PROJECT_ID" /hermes      "$TMP/ops-hermes.json"
apply_secret company-ops-secrets "$OPS_NS" "$TMP/ops-root.json" "$TMP/ops-infra.json" "$TMP/ops-hermes.json"

echo "[4/6] Fetching steward paths..."
for s in engineering-agent engineering-opencode engineering-opencode-direct; do
  fetch "$OPS_PROJECT_ID" "/infra/steward/${s}" "$TMP/steward-${s}.json"
  apply_secret "company-ops-steward-${s}" "$OPS_NS" "$TMP/steward-${s}.json"
done

echo "[5/6] Fetching buildmyhouse-app project -> buildmyhouse-dev-secrets..."
fetch "$APP_PROJECT_ID" / "$TMP/app.json"
apply_secret buildmyhouse-dev-secrets "$APP_NS" "$TMP/app.json"

echo "[6/6] Recreating cluster-issued SA-token Secrets + live-sync manifests..."
kubectl apply -f "$REPO_ROOT/k8s/builder-rbac.yaml" >/dev/null
kubectl apply -f "$REPO_ROOT/k8s/buildmyhouse-dev-deployer-rbac.yaml" >/dev/null
if kubectl get crd infisicalstaticsecrets.secrets.infisical.com >/dev/null 2>&1; then
  kubectl apply -f "$REPO_ROOT/k8s/infisical-sync.yaml" >/dev/null
  kubectl apply -f "$REPO_ROOT/k8s/buildmyhouse-dev-infisical-sync.yaml" >/dev/null
  echo "Infisical Secrets Operator detected — live-sync manifests applied."
else
  echo "Infisical Secrets Operator CRDs not found — skipped live-sync manifests."
  echo "Secrets are materialized statically; install the operator when ready"
  echo "(see docs/SECRETS-INFISICAL.md)."
fi

echo "Done. All Secrets bootstrapped from Infisical."

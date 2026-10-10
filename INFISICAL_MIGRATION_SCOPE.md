# Infisical Migration Scope: Secrets Infrastructure

**Status:** Implemented for company-os bootstrap (2026-10-10). The runtime
source of truth, inventory, gap list, and new-host procedure live in
[`docs/SECRETS-INFISICAL.md`](docs/SECRETS-INFISICAL.md) — read that first.
This document records the design decisions and what remains (Nahar actions).

## Problem (historical context)

Production `.env` files used to hold plaintext secrets (API keys, DB
passwords, Discord tokens, GitHub App private keys, AWS/R2 credentials):
world-readable in containers, duplicated across copies, protected only by
gitignore, with no programmatic rotation or audit trail. Two Steward MCP
bearer tokens were previously exposed via git.

## Implemented design

**Layer 1 — Projects and paths (API-level access control).** Infisical
instance at `https://eu.infisical.com`, environment `dev`:

1. **Build My house** project (`8806c2b0-73d2-4bea-8537-5b874c5ff592`) —
   company-os runtime secrets, separated by path:
   - `/` — shared/cross-cutting (Axiom dashboard, buildmy.house MCP test account)
   - `/infra` — GitHub App (incl. the PEM as `GITHUB_APP_PRIVATE_KEY`), R2,
     Cloudflare, Neon, Steward, model-provider keys, Claude OAuth
   - `/hermes` — Discord config, Postgres role passwords + connection strings,
     hermes' own Steward token
   - `/infra/steward/{engineering-agent,engineering-opencode,engineering-opencode-direct}`
     — per-agent Steward identities
2. **buildmyhouse-app** project (`58b43b81-effb-4392-a937-46f2448efb78`) —
   app runtime (JWT, app R2/Resend/Axiom keys), synced into the
   `buildmyhouse-dev` namespace only.

Path separation inside one project is the current enforcement boundary; a
second project with a disjoint machine identity for `/infra` is the upgrade
path if stronger isolation is ever needed.

**Layer 2 — Runtime injection.**

- Kubernetes (primary): Infisical Secrets Operator syncs every path into k8s
  Secrets (`k8s/infisical-sync.yaml`, `k8s/buildmyhouse-dev-infisical-sync.yaml`);
  workloads consume them via `envFrom` of a single merged Secret
  (`company-ops-secrets`) or dedicated Secrets (`company-ops-steward-*`).
  On a new host the same Secrets are materialized statically by
  `scripts/bootstrap-secrets.sh` before the operator is installed, so
  workload startup never depends on operator availability.
- Entrypoints (secondary/direct): `scripts/entrypoint.sh` and
  `scripts/engineering-entrypoint.sh` call
  `scripts/fetch-infisical-secrets.js` with the Universal Auth bootstrap
  identity and source the result — used when the bootstrap env vars are set
  (e.g. local docker runs).

**Layer 3 — The only manual credential on a new host.**
`infisical-bootstrap-credentials` (Universal Auth machine identity for
company-os; keys `clientId`, `clientSecret`), exported as
`INFISICAL_UNIVERSAL_AUTH_CLIENT_ID` / `_CLIENT_SECRET` when running
`scripts/bootstrap-secrets.sh`. Everything else — `.env` contents,
`certs/buildmyhouse-engineering-app.pem`,
`certs/buildmyhouse-test-account.env`, `mcp-workers.json` (verified to
contain no secrets), and all k8s Secrets — comes from Infisical or from
declarative manifests.

**Layer 4 — Constitutional boundary (policy, unchanged).**
Hermes must never access infrastructure-critical secrets beyond what its
runtime path grants. The `/hermes` vs `/infra` path split plus per-agent
Steward identities enforce this at the API level; keep it that way when
adding new secrets.

## What remains (Nahar actions)

1. **Import the three gap keys** into `Build My house`/dev `/hermes`:
   `AXIOM_DATASET`, `AXIOM_ENDPOINT`, `PM_DATABASE_URL` (see gap list in
   docs/SECRETS-INFISICAL.md). Until then a new host cannot serve
   `PM_DATABASE_URL` from Infisical.
2. **Machine identity provisioning** for new hosts (Universal Auth identity
   with read access to both projects).
3. Rotation/audit runbook is covered by the rotation section of
   docs/SECRETS-INFISICAL.md; emergency "Infisical down" access is
   intentionally undocumented (re-provision from a healthy host).
4. Git-history scrubbing of old plaintext secrets remains out of scope.

## Out of scope / superseded

The older proposal in this file (two dedicated projects, `infisical run --token`
entrypoint wrapping, MCP-secrets-server integration, worker-per-session
identities) is superseded by the implemented design above. Do not re-implement
it; extend docs/SECRETS-INFISICAL.md instead.

# Container images

All repo-owned images live in the in-cluster registry (`k8s/registry.yaml`,
NodePort `localhost:30500`) under `localhost:30500/<repo>`. Build and push
every one of them from any checkout:

```bash
scripts/build-push-images.sh              # build+push all, tagged with git short SHA
scripts/build-push-images.sh --only engineering
scripts/build-push-images.sh --dry-run    # print commands only
scripts/build-push-images.sh --print-digests   # emit image@sha256 refs for pinning manifests
```

Each image gets the immutable short-`git rev-parse --short HEAD` tag plus the
mutable tag its manifests reference. Env overrides: `REGISTRY` (default
`localhost:30500`), `WORKSPACE_CONTEXT` (git URL for `Dockerfile.engineering`'s
named `shared` context; pin to `URL#<40-hex-sha>` for reproducible builds),
`AGENT_FLAVOR` (default `claude`), `INSTALL_BROWSER` (default `true`).
Requires only docker (buildx), git, and a reachable registry — no host paths.
The in-cluster BuildKit route (`builder_build_and_push`, see
docs/DEPLOY-ENGINEERING.md) remains the sanctioned path for routine
agent-driven builds; this script is the fresh-host/local-up path.

## Image map

| Image (localhost:30500/…) | Tag(s) in k8s/ | Consumed by | Dockerfile | Context | Extra | Who builds |
|---|---|---|---|---|---|---|
| `company-os` | `@sha256:…` (company-ops.yaml), `:dns-remediation` (dns-healthcheck.yaml) | hermes-gateway Deployment; dns-healthcheck CronJob | `Dockerfile` | repo root | — | build-push-images.sh / builder MCP |
| `company-os-engineering` | `:hive-bid-8b29112` (engineering.yaml, engineering-opencode.yaml, engineering-opencode-direct.yaml), `@sha256:…` (engineering-build-dispatcher.yaml, hermes-build-dispatcher.yaml) | engineering-agent + opencode variants; both build dispatchers | `Dockerfile.engineering` | repo root | named `shared` context = `WORKSPACE_CONTEXT`; args `AGENT_FLAVOR`, `INSTALL_BROWSER` | build-push-images.sh / builder MCP |
| `company-os-hive` | `:events` (hive.yaml) | hive-coordinator | `hive/Dockerfile` | repo root | — | build-push-images.sh |
| `company-os-pm-agent` | `:latest` (pm-agent.yaml — **not** in kustomization; apply separately if wanted) | pm-agent | `pm-agent/Dockerfile` | repo root | — | build-push-images.sh |
| `company-os-browser-adversary` | `:latest` (browser-adversary.yaml) | browser-adversary | `Dockerfile.browser-adversary` | repo root | — | build-push-images.sh |

Upstream images, never built here: `postgres:16-alpine` (postgres.yaml),
`registry:2` (registry.yaml).

If you change a mutable tag in a manifest, update the matching entry in
`scripts/build-push-images.sh`'s image table in the same commit.

## Convention for RM3 (`k8s/postgres-backup.yaml`)

postgres-backup.yaml still references `docker.io/library/company-ops-company-ops:latest`
— a docker-compose-style local-only name that does not exist in the cluster
registry. RM3 owns that file; when touching it, point it at
`localhost:30500/company-os@sha256:<digest>` (digest from
`scripts/build-push-images.sh --only company-os --print-digests`) or at
minimum `localhost:30500/company-os:latest`. Do not introduce new
`docker.io/library/…` references anywhere.

## Not reproducible from this repo

- `localhost:30500/buildmyhouse-app|mcp|luxcore:dev-…` (k8s/buildmyhouse-dev-*.yaml):
  built from the separate BuildMy-house/app repo by the dev deployer
  (scripts/dev-deploy-poller.js via the in-cluster BuildKit job); not built
  from this repo.
- `company-os-engineering` requires the private `BuildMy-house/workspace` repo
  as its named `shared` build context (`WORKSPACE_CONTEXT`) — company-os alone
  is not enough to rebuild it.
- Digest-pinned refs (company-ops.yaml, both build dispatchers) are pinned by
  whoever upgrades those Deployments after a build; `--print-digests` exists
  for exactly that step.

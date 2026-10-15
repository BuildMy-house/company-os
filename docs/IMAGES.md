# Container images

All repo-owned images live in the in-cluster registry (`k8s/base/registry/registry.yaml`,
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
| `company-os-engineering` | `:ticket-std-714c938@sha256:24d6efe3f75630b250045e482af3dbf20156d42dddf70b58c6ab7c46a73f27ed` (engineering.yaml, engineering-opencode.yaml, engineering-opencode-direct.yaml), `@sha256:…` (engineering-build-dispatcher.yaml, hermes-build-dispatcher.yaml) | engineering-agent + opencode variants; both build dispatchers | `Dockerfile.engineering` | repo root | named `shared` context = `WORKSPACE_CONTEXT`; args `AGENT_FLAVOR`, `INSTALL_BROWSER` | build-push-images.sh / builder MCP |
| `company-os-hive` | `:ticket-std-714c938@sha256:c2b02124…` (hive.yaml) | hive-coordinator | `hive/Dockerfile` | repo root | — | build-push-images.sh |
| `company-os-pm-agent` | `:latest` (pm-agent.yaml, included by `k8s/overlays/buildmy-prod`) | pm-agent | `pm-agent/Dockerfile` | repo root | — | build-push-images.sh |
| `company-os-browser-adversary` | `:latest` (browser-adversary.yaml) | browser-adversary | `Dockerfile.browser-adversary` | repo root | — | build-push-images.sh |
| `company-os-mcp-gateway` | digest-pinned (mcp-gateway.yaml — **not** in kustomization; admin applies once) | mcp-gateway | `Dockerfile.mcp-gateway` | repo root | — | `builder_build_and_push` only (see docs/MCP-GATEWAY.md) |

Upstream images, never built here: `postgres:16-alpine` (postgres.yaml),
`registry:2` (registry.yaml).

If you change a mutable tag in a manifest, update the matching entry in
`scripts/build-push-images.sh`'s image table in the same commit.

## Digest pins that must move together

`localhost:30500/company-os@sha256:<digest>` is referenced by three manifests:
`k8s/base/hermes/company-ops.yaml`, `k8s/base/backups/postgres-backup.yaml` and
`k8s/base/backups/hermes-data-backup.yaml`. The backup CronJobs run `company_ops.backup`
from the same image, so after any rebuild that changes `company_ops/` (the
`--hermes-data` mode exists only in images built from this branch or later)
get the new digest from `scripts/build-push-images.sh --only company-os
--print-digests` and update all three in the same commit. An older pinned image
silently ignores `--hermes-data` and would run the Postgres dump instead.
Do not introduce `docker.io/library/…` references for repo-owned images.

## Not reproducible from this repo

- `localhost:30500/buildmyhouse-app|mcp|luxcore:dev-…` (k8s/projects/buildmyhouse-dev/*.yaml):
  built from the separate BuildMy-house/app repo by the dev deployer
  (scripts/dev-deploy-poller.js via the in-cluster BuildKit job); not built
  from this repo.
- `company-os-engineering` requires the private `BuildMy-house/workspace` repo
  as its named `shared` build context (`WORKSPACE_CONTEXT`) — company-os alone
  is not enough to rebuild it. The builder container can't see the client's
  git credentials, so export `GIT_AUTH_TOKEN` (a GitHub App installation token,
  see `scripts/github-app-token.js`) before running the script; it is passed to
  BuildKit as a build secret and never lands in an image layer.
- Digest-pinned refs (company-ops.yaml, both build dispatchers) are pinned by
  whoever upgrades those Deployments after a build; `--print-digests` exists
  for exactly that step.

## Tag scheme and why `buildmy-prod` has no `images:` entries (COMP-7)

Production refs should be immutable: `<image>:<component>-<git-sha>@sha256:<digest>`
(tag for humans, digest for the cluster). The Hermes base in `Dockerfile` is
pinned by digest (`nousresearch/hermes-agent@sha256:444b4259...`, resolved
2026-10-13); bump it deliberately.

The kustomize images transformer matches by image *name* and rewrites every
occurrence. `company-os-engineering` and `company-os` each run with several
different refs in production, so one `images:` entry would change the render.
The manifests therefore stay the source of truth and the `buildmy-prod` render
stays identical; neutral overrides live in the example overlays.

## Repo vs live drift (live inventory ns company-ops, 2026-10-13)

| Workload | Repo | Live (2026-10-13) | Status |
|---|---|---|---|
| engineering-opencode-direct | `company-os-engineering:ticket-std-714c938@sha256:24d6efe3f75630b250045e482af3dbf20156d42dddf70b58c6ab7c46a73f27ed` | `company-os-engineering:runner-guard-b24cae3@sha256:33285f0ec1347a4f30b02576c4c693dfb9051e7b9018cafd1f4f269c3348f215` (verified 2026-10-15; matches repo) | in sync: no pending rollout |
| hermes-gateway | `company-os:hermes-e5fce28@sha256:f905ee5424f27a9fb676dec4f2fa73e5db43da2082129b589aec9e90692a3b55` | `company-os:hermes-e5fce28@sha256:f905ee5424f27a9fb676dec4f2fa73e5db43da2082129b589aec9e90692a3b55` after rollout | Built from main e5fce28 (includes dream-audit Hindsight step, SOUL `due_at` rule). Live ran `hermes-188e347`, an ancestor of main, so this only adds #46, #48, #50 and #52. The backup CronJobs use `company-os@sha256:6b458067…` (backup-4a589a8). |

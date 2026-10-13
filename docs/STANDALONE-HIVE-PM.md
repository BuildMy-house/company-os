# Stand up hive + pm-agent for another project

Practical guide for deploying the hive coordinator and pm-agent for a
different project from the same manifests and images, with no fork. Background
and coupling evidence: [COMPOSABILITY-PLAN.md](COMPOSABILITY-PLAN.md),
[COMPOSABILITY-AUDIT.md](COMPOSABILITY-AUDIT.md). The template overlay is
`k8s/overlays/example-standalone`; the scaffold script is
`scripts/new-project-overlay.sh`.

## Prerequisites

- A Kubernetes cluster you can deploy to. Deployment happens through your
  normal deploy path; nothing in this guide is run from an agent session.
- An image registry reachable from the cluster nodes, where you will push the
  two project images (hive, pm-agent).
- A Postgres reachable from the cluster. Hive uses it optionally
  (`COMPANY_DATABASE_URL`; without it hive falls back to an in-memory store,
  fine for a trial, not for anything durable). Pm-agent requires it
  (`PM_DATABASE_URL`).
- Secrets created out of band, never committed to this repo:
  - `<project>-secrets` — read by hive via `envFrom` (put `COMPANY_DATABASE_URL`
    and, if you want telemetry, `AXIOM_TOKEN`/`AXIOM_DATASET` here) and by the
    pm-agent gateway (`PM_AGENT_AUTH_TOKEN`).
  - `pm-agent-secrets` — read by the pm-agent container itself
    (`PM_DATABASE_URL`, `PM_AGENT_LLM_API_KEY`). This name is fixed in the base
    manifest; fold it into `<project>-secrets` with an extra patch if you want
    a single secret per project.

## Steps

### 1. Scaffold the overlay

    scripts/new-project-overlay.sh <project> <namespace> <registry-host>

Example: `scripts/new-project-overlay.sh acme acme-ops registry.example.internal:5000`.

This copies `k8s/overlays/example-standalone` to `k8s/overlays/<project>` and
replaces four tokens: namespace `acme-ops` becomes `<namespace>`, secret
`acme-secrets` becomes `<project>-secrets`, project name `acme` becomes
`<project>`, and registry `registry.example.internal:5000` becomes
`<registry-host>`. It refuses to overwrite an existing directory, then runs the
render gate (see step 5) if a kustomize renderer is available.

### 2. Edit the overlay's parameters

In `k8s/overlays/<project>/kustomization.yaml`:

- Replace both `replace-with-immutable-tag` values with the immutable tags you
  pushed in step 3.
- Set `HIVE_NAME` and `HIVE_DESCRIPTION` in the hive patch to your project's
  identity (they appear on the agent card).
- Optional: patch `HIVE_URL` (default `http://hive-coordinator:4100`, correct
  for in-namespace callers) or `ENGINEERING_A2A_URL` (default
  `http://engineering-agent:8001`, only relevant if you also deploy the
  engineering pool).
- Optional: patch the pm-agent container with `PM_AGENT_CONTENT_DIR` (step 4).

The namespace transformer already rewrites every Role/RoleBinding subject and
all `namespace:` fields to `<namespace>`, so nothing else needs touching for
hive + pm-agent.

### 3. Build and push the images

Build from the repository root (both Dockerfiles expect the repo root as build
context), tag immutably (git SHA, never `latest`):

    docker buildx build -f hive/Dockerfile -t <registry-host>/<project>-hive:<git-sha> .
    docker buildx build -f pm-agent/Dockerfile -t <registry-host>/<project>-pm-agent:<git-sha> .

Push both to `<registry-host>`. The overlay's `images:` transformer rewrites
the base's `localhost:30500/...` refs to these names.

### 4. Per-project content and identity

Pm-agent's persona and refusal text are per-project files selected by env:

- Author `pm-agent/content/<project>/SOUL.md` and
  `pm-agent/content/<project>/refusal.txt`. Copying
  `pm-agent/content/buildmy-house/` as a starting point is fine; the content
  must be rewritten for your project.
- The content is baked into the pm-agent image (`COPY pm-agent/` in the
  Dockerfile), so the files must exist in the checkout before you build, and
  you set `PM_AGENT_CONTENT_DIR=/opt/company-ops/pm-agent/content/<project>`
  on the pm-agent container (an `op: add` env patch in the overlay, same
  pattern as the `HIVE_NAME` patch).

Hive's identity is read from env at runtime (`hive/config/runtime.exs`):
`HIVE_NAME`, `HIVE_DESCRIPTION`, `HIVE_URL`, `ENGINEERING_A2A_URL`. The
template overlay already sets the first two; the defaults for the other two are
correct for a same-namespace deployment.

### 5. Render and gate

    FORBID_LITERALS="buildmyhouse company-ops localhost:30500 homely" \
      SKIP_BASELINE=1 scripts/verify-kustomize-render.sh k8s/overlays/<project>

Must print `RENDER OK`. `FORBID_LITERALS` fails the gate if any BuildMy token
survives in the render; `SKIP_BASELINE=1` is correct here because a new
project overlay is not supposed to equal the production render.

### 6. Deploy

Render the manifest (`kustomize build k8s/overlays/<project>` or the verify
script's output) and hand it to whoever owns cluster access for your
environment — apply through your normal deploy path. Do not apply from a
planning/agent session; there is no cluster access there by design.

### 7. Verify

- Hive agent card: `GET http://<hive-host>:4100/.well-known/agent-card.json`
  must show your `HIVE_NAME`/`HIVE_DESCRIPTION`/`HIVE_URL` (this is also the
  kubelet readiness probe path, so a Ready pod already proves it).
- Pm-agent: `GET http://<pm-agent-host>:4200/healthz` returns 200 (readiness
  probe). Externally it sits behind the nginx gateway (NodePort 30420),
  authenticated with `PM_AGENT_AUTH_TOKEN`.

## Known limits

- `hermes/config.yaml` still contains a BuildMy Steward MCP URL literal. This
  only matters if you deploy the hermes gateway for the new project; hive and
  pm-agent do not read that file.
- Persona content (`SOUL.md`, refusal text) must be authored per project. The
  repo only makes the file selectable via `PM_AGENT_CONTENT_DIR`; it cannot be
  generic.
- The engineering worker pool is a separate overlay, not part of this guide:
  see `k8s/overlays/example-standalone-engineering` (hive + pm-agent stay as
  above; wire `ENGINEERING_A2A_URL` only if you also deploy the pool in the
  same namespace).
- `pm-agent-secrets` keeps its fixed base-manifest name (see prerequisites);
  only `<project>-secrets` is renamed by the overlay.
- The scaffold rewrites the template's four tokens only. Anything beyond
  hive + pm-agent (postgres manifests, registry, dev-cluster stacks) is not
  included in the overlay and must be added deliberately if needed.

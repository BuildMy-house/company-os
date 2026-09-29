# Engineering container build / deploy / rollback

## What the engineering container is

The `engineering-agent` runs a manager persona plus `ai-cli-mcp` worker
dispatch for Hermees's own dispatch path. The production Claude flavor keeps
the existing Claude manager + OpenCode worker arrangement. A slim OpenCode
flavor uses the same manager facade and worker path without installing Claude
or Codex. It is defined by
`Dockerfile.engineering` at the repo root and started by
`scripts/engineering-entrypoint.sh`, which syncs BuildMy-house repos then
execs `mcp-proxy` serving the manager facade over `ai-cli-mcp`.

The flavor is a build-time choice; manager versus worker is a runtime pattern,
not a second codebase:

```bash
# Current production flavor
docker build -f Dockerfile.engineering --build-arg AGENT_FLAVOR=claude \
  --build-context "shared=https://x-access-token:$(gh auth token)@github.com/BuildMy-house/workspace.git" \
  -t localhost:30500/company-os-engineering:<tag> .

# Slim OpenCode manager/workers flavor
docker build -f Dockerfile.engineering --build-arg AGENT_FLAVOR=opencode \
  --build-arg INSTALL_BROWSER=false \
  --build-context "shared=https://x-access-token:$(gh auth token)@github.com/BuildMy-house/workspace.git" \
  -t localhost:30500/company-os-engineering-opencode:<tag> .
```

Set `AGENT_FLAVOR=opencode`, `AGENT_ROLE=manager`, and
`RUNNER_AGENT=opencode` on an OpenCode deployment. `ai-cli-mcp` remains the
stable manager-facing dispatch service; it is not a separate worker runtime.

## How it actually runs in production

The container runs as a **Kubernetes Deployment** named `engineering-agent`
in namespace `company-ops`, on a local **k3s** cluster. The kubeconfig is
`~/.kube/config`. Set `KUBECONFIG=/home/nahar/.kube/config` explicitly before
running `kubectl` — do not let it fall back to merging
`/etc/rancher/k3s/k3s.yaml`, which requires root to read and produces
permission warnings.

```bash
export KUBECONFIG=/home/nahar/.kube/config
```

Images are pulled from an **in-cluster registry** (`registry:2`), deployed
via `k8s/registry.yaml` as a Deployment+PVC+NodePort Service in
`company-ops`, exposed at `localhost:30500` on the node. `kubectl set image`
triggers a normal kubelet pull from it — there is no host-level `k3s ctr`
step and no `sudo` involved anywhere in this flow. k3s's containerd trusts
`localhost` as insecure/loopback by default, so pushing/pulling
`localhost:30500/...` needs no registries.yaml or containerd config either.

There is **no fixed tag convention** (no `:candidate`/`:latest`/`:previous`
alias enforced anywhere). A redeploy means building a new, uniquely-named
tag and pointing the Deployment at it — not overwriting a shared tag. Check
what tag is currently live at any time with:

```bash
kubectl get deployment engineering-agent -n company-ops \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

## Branch discipline: build from `prod`, not `main`

`main` is the integration branch — agents push verified work there directly,
no approval needed. `prod` is the branch that represents what is actually
allowed to run live; merging into it, and any action that deploys a new
image from it, requires explicit user approval first. A real deploy
therefore always builds from `prod`, never from whatever happens to be
checked out on `main` at the moment:

```bash
git checkout prod
# or, if staying on another branch, confirm it matches prod's tip first:
git rev-parse HEAD
git rev-parse origin/prod
```

Only proceed with the build below once the checkout you're building from is
`prod`'s current tip (or bail and ask for the `main`→`prod` merge to be
approved first). Building from `main` directly skips the approval gate this
branch split exists to enforce.

## Build context: `Dockerfile.engineering` pulls from the private `workspace` repo

`Dockerfile.engineering` uses a `docker buildx` **git-URL build context**
(requires Docker 23+/buildx) named `shared` to pull the canonical
`agent-manager.md` and related files from the separate private
`BuildMy-house/workspace` GitHub repo at build time. This replaced an older
approach that used local-directory build contexts
(`--build-context manager-def=../.claude/agents --build-context
skills-src=../.agents/skills`) assuming a now-removed monorepo layout — that
layout no longer exists.

Because `workspace` is private, the git URL must carry an authenticated
token, e.g. a GitHub token from `gh auth token`:

```bash
docker build -f Dockerfile.engineering \
  --build-context "shared=https://x-access-token:$(gh auth token)@github.com/BuildMy-house/workspace.git" \
  -t localhost:30500/company-os-engineering:<tag> .
```

Pick `<tag>` as something unique and traceable (e.g. a date or short git
SHA) — it does not need to follow any reserved name.

## Push to the registry

```bash
docker push localhost:30500/company-os-engineering:<tag>
```

## Deploy

Point the running Deployment at the newly-pushed tag:

```bash
kubectl set image deployment/engineering-agent \
  engineering-agent=localhost:30500/company-os-engineering:<tag> \
  -n company-ops
kubectl rollout status deployment/engineering-agent -n company-ops
```

`kubectl rollout status` blocks until the new pod is healthy and ready, or
reports the rollout failure.

## Rollback

Two options:

1. Undo the most recent rollout (uses Kubernetes' own rollout history):

   ```bash
   kubectl rollout undo deployment/engineering-agent -n company-ops
   ```

2. Point explicitly at a known-good older tag, if it's still present in the
   registry:

   ```bash
   curl -s http://localhost:30500/v2/company-os-engineering/tags/list
   kubectl set image deployment/engineering-agent \
     engineering-agent=localhost:30500/company-os-engineering:<older-tag> \
     -n company-ops
   kubectl rollout status deployment/engineering-agent -n company-ops
   ```

## Image cleanup

Old/unused image tags accumulate in the registry over time since builds are
never automatically pruned. Operators should periodically check for tags no
longer referenced by any Deployment and remove them via the registry API
(the `registry:2` image needs `REGISTRY_STORAGE_DELETE_ENABLED=true`, already
set in `k8s/registry.yaml`, and a garbage-collect pass to actually reclaim
disk):

```bash
curl -s http://localhost:30500/v2/company-os-engineering/tags/list
digest=$(curl -sI http://localhost:30500/v2/company-os-engineering/manifests/<unused-tag> \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  | grep -i docker-content-digest | awk '{print $2}' | tr -d '\r')
curl -X DELETE http://localhost:30500/v2/company-os-engineering/manifests/$digest
kubectl exec -n company-ops deployment/registry -- registry garbage-collect /etc/docker/registry/config.yml
```

## Agent-triggered build+push: `builder-manager` (rootless BuildKit Job), no docker socket anywhere

Everything above this section describes a human/CI running `docker build`/
`docker push` from a workstation. `scripts/builder-manager-mcp.js` gives
Hermes/the engineering-agent a way to trigger the same kind of build+push
**from inside a k3s pod**, without ever handing that pod a docker socket or
persistent registry credentials — the actual boundary this whole build
pipeline exists to keep:

- The engineering-agent pod and the hermes-gateway pod never hold a docker
  socket, `k3s ctr` access, `sudo`, or long-lived push credentials. All they
  can do is ask the Kubernetes API to create a **Job**.
- `builder_build_and_push(context_ref, dockerfile_path, image_repo,
  image_tag, contexts?, build_args?)` creates a short-lived Job running a
  rootless `moby/buildkit` image (pinned by digest) via
  `buildctl-daemonless.sh` in `company-ops`. BuildKit builds directly from a
  **git context** (`context_ref`, e.g.
  `https://github.com/BuildMy-house/company-os.git#<40-hex-sha>` — a full
  commit SHA is required, a mutable branch name like `#main` is rejected)
  and pushes straight to `registry.company-ops.svc.cluster.local:5000` — no
  docker daemon involved on either end. Optional `contexts: { <name>: <url>
  }` adds **named additional build contexts** (the same primitive
  `docker buildx build --build-context` uses, native to plain BuildKit's
  dockerfile.v0 frontend) — this is what makes `Dockerfile.engineering`
  buildable through this tool; see "Named build contexts" below.
- Verified live in this cluster on 2026-09-29: rootless BuildKit runs here
  with **no privileged workaround** — no `privileged: true`, no added Linux
  capabilities, no `/dev/fuse` hostPath, no `hostUsers`/user-namespaces
  (tried, failed on this node's `newuidmap`/subuid setup, reverted). The
  Job template sets `securityContext.seccompProfile: Unconfined`, the
  `container.apparmor.security.beta.kubernetes.io/buildkit: unconfined` pod
  annotation, a non-root `runAsUser`, and env
  `BUILDKITD_FLAGS=--oci-worker-no-process-sandbox` (works around a
  "mount proc: operation not permitted" failure on Dockerfile `RUN` steps
  under this node's nested mount-namespace restrictions) — that
  combination alone was sufficient for full builds to succeed. Confirmed
  with two real Jobs submitted directly against this cluster's Jobs API
  using the `builder-manager` SA token: a baseline single-context build
  with a real `RUN` step, and a named-additional-context build resolving
  two independently-pinned commits of the same public repo (asserted via
  an in-build content diff, not just log inspection) — both pushed
  successfully to the in-cluster registry, confirmed independently via a
  direct `GET /v2/<repo>/tags/list`, with no leftover Jobs afterward. Both
  test fixtures live in this repo (`Dockerfile.buildkit-selftest-basic`,
  `Dockerfile.buildkit-selftest`) and need only `company-os`'s own public
  read access — this decouples verifying the BuildKit mechanism itself
  from the GitHub-App-token/Secrets-RBAC path below, which is separately
  still blocked.
- The Job pod itself runs with `automountServiceAccountToken: false` — it
  has zero Kubernetes API access; it only ever talks to the git remote and
  the registry over plain HTTP/HTTPS.
- The MCP tool (not the Job) authenticates to the Kubernetes API as a
  **dedicated `builder-manager` ServiceAccount** (`k8s/builder-rbac.yaml`),
  deliberately separate from the shared `company-ops` SA that
  `container_manager`/`k8s_deployment` use. Its Role can
  `create`/`get`/`list`/`watch`/`delete` **Jobs**, read their pods' logs,
  and `create`/`delete` (never `get`/`list`/`watch`/`patch`) **Secrets** —
  the last one added for the ephemeral per-build git-token Secret described
  below; it still cannot read any Secret, including ones it creates itself,
  back out. It cannot touch Deployments at all. This SA's bound token is
  mounted at a distinct path (`/var/run/secrets/builder-manager/token`, via
  a Secret + volume added to both the `hermes-gateway` and
  `engineering-agent` Deployments), separate from the pod's default
  in-cluster SA token path.
- The tool polls the Job to completion, fetches the BuildKit pod's logs on
  either outcome (masking any injected git token before returning them —
  see "Named build contexts" below), deletes the Job (best-effort;
  `ttlSecondsAfterFinished: 600` on the Job spec is the backstop if the
  delete call itself fails), and independently confirms the pushed tag
  actually exists in the registry (`GET /v2/<repo>/tags/list`) before
  reporting success — it does not just trust BuildKit's own exit status.
- Wired into `scripts/generate-agent-mcp-config.js` for both Hermes and
  Claude's engineering-manager, and registered directly in
  `hermes/config.yaml` as `builder_manager`. It has its own gate, separate
  from `container-manager`/`registry-manager`/`hermes-messenger`'s generic
  "are we running in this k3s Deployment at all" check: it only gets
  advertised when **both** `/var/run/secrets/builder-manager/token` exists
  **and** `scripts/builder-manager-mcp.js` is actually present in the
  image. A runner must never advertise a tool it cannot launch — see
  "Each runner only advertises tools it can launch" below for why that
  second half of the check exists. Unlike
  `container-manager`/`registry-manager`/`hermes-messenger`, it is **not**
  added to the OpenCode skip-list in `generate-agent-mcp-config.js` — its
  own RBAC is already narrow enough that OpenCode workers dispatched from
  engineering-manager can safely trigger builds too.

### Named build contexts and git credential handling

`contexts` values (and `context_ref` itself) must be bare, credential-free
`https://` URLs pinned to a full 40-hex commit SHA — enforced by
`builder-manager-mcp.js`'s own validation, not left to caller discipline.
For the private `BuildMy-house/*` repos specifically (recognized by URL
pattern), the tool mints its own short-lived GitHub App installation token
(reusing `github-app-token.js`, the same mechanism `sync-repo.sh` already
uses) and delivers it to the Job pod via a dedicated, per-build Kubernetes
Secret referenced with `secretKeyRef` — the token itself never appears in
the Job manifest's own text, only a reference to the Secret's name/key
does, so it isn't visible to anything that can merely `get`/`list` Jobs.
The Secret is deleted alongside the Job on every exit path (success,
failure, or crash before Job creation).

Callers should never pass credentials in a `context_ref`/`contexts` value
themselves — the tool's validation rejects any URL containing embedded
credentials, precisely so callers don't need to handle or mint tokens.

**Residual, documented limitation**: BuildKit's own git-source log line
echoes the resolved (credentialed) URL into the *pod's* stdout logs. The
tool masks the token in everything it returns to its own caller, but the
raw pod log is still visible for the Job's short lifetime to anything with
`pods/log` read in `company-ops` — bounded by the Job's
`ttlSecondsAfterFinished` (10 min) and the token's own ~1h GitHub-imposed
expiry either way. No fully-suppressing alternative was found in
BuildKit's git-source op for the dockerfile frontend; this is a real,
accepted tradeoff, not an oversight.

### Each runner only advertises tools it can launch

A server showing up in `tools/list` is not proof it works — if its script
was never `COPY`'d into `Dockerfile.engineering`, the tool spawn fails the
moment something actually calls it, and that failure surfaces far from
the real cause (a missing `COPY` line landed and shipped unnoticed
because nothing checked the two facts against each other). Two
independent defenses now cover this:

1. **Structural, at config-generation time**: `generate-agent-mcp-config.js`
   only adds `builder-manager` to the servers list when its script file
   exists on disk in addition to its token being mounted (see above) —
   the same principle should be applied to any future MCP server this
   file adds.
2. **Runnable check**: `scripts/check-advertised-mcp-tools.js` reads the
   generated `~/.claude.json`/`~/.config/opencode/opencode.json` and
   confirms every local (`node <path>`) server's script actually exists.
   `scripts/engineering-entrypoint.sh` runs it (non-fatal, logs a WARN)
   right after `generate-agent-mcp-config.js` on every container boot. Run
   it by hand inside a live pod to diagnose a suspected advertised-but-
   broken tool:

   ```bash
   node /opt/company-ops/scripts/check-advertised-mcp-tools.js
   ```

**Same class of gap found again, 2026-09-29**: this session's own live MCP
connection list showed `hermes-messenger` as `CONNECTION_CLOSED` —
`scripts/hermes-messenger-mcp.js` is on disk and correctly gated in
`generate-agent-mcp-config.js` (same generic in-cluster-SA check as
`container-manager`/`registry-manager`), but was never added to
`Dockerfile.engineering`'s `COPY scripts/...` list, so the running image
never had the script to launch. Fixed by adding the missing `COPY` line.
Cross-checked every other script `generate-agent-mcp-config.js` and
`hermes/config.yaml` reference against `Dockerfile.engineering`'s `COPY`
list after this fix — no further gaps found as of this commit.

### Required live setup before this can actually build anything

`k8s/builder-rbac.yaml` (the ServiceAccount/Role/RoleBinding/token Secret)
and the two Deployment volume-mount changes must be applied to the live
cluster, and the affected Deployments restarted to pick up the new volume
mounts, before `builder_build_and_push` can authenticate at all:

```bash
kubectl apply -f k8s/builder-rbac.yaml
kubectl apply -f k8s/company-ops.yaml
kubectl apply -f k8s/engineering.yaml
kubectl -n company-ops rollout restart deployment hermes-gateway
kubectl -n company-ops rollout restart deployment engineering-agent
kubectl -n company-ops rollout status deployment hermes-gateway
kubectl -n company-ops rollout status deployment engineering-agent
```

**Confirmed live 2026-09-29** (from inside the running `engineering-agent`
pod, via `SelfSubjectAccessReview` probes as the `builder-manager` SA —
`kubectl` itself is not available there): `/var/run/secrets/builder-manager/token`
is mounted in this pod. Its Role currently grants exactly
`create`/`get`/`list`/`watch`/`delete` on `jobs.batch` and `get`/`list`/
`watch` on `pods`/`pods/log` — confirmed all `true`. **`secrets: ["create",
"delete"]` (this change's addition to `k8s/builder-rbac.yaml`) is NOT yet
applied** — `SelfSubjectAccessReview` for `create`/`delete`/`get`/`list` on
`secrets` all came back `false`. The Jobs/pods-log portion of the RBAC was
also independently exercised end-to-end with two real BuildKit build+push
runs (see the "rootless BuildKit runs here" bullet above) — that part of
the mechanism is proven, not just RBAC-probed. The Secrets addition itself
has not been applied to the live cluster and this pod has no way to apply
it (no `kubectl`, and the default `company-ops` SA was separately confirmed
to lack `get`/`patch`/`update` on `roles`/`rolebindings` — this pod cannot
self-escalate its own RBAC, by design). Applying it is an out-of-pod
action for whoever administers this cluster. `git status` confirms
`scripts/builder-manager-mcp.js` was absent from the image running at the
time of writing (a prior gap now closed in source by this change) — a new
image build+push+`container_upgrade` is still required for the running
container to pick it up, in addition to the Secrets RBAC being applied.

```bash
kubectl -n company-ops auth can-i create jobs.batch \
  --as=system:serviceaccount:company-ops:builder-manager   # expect yes
kubectl -n company-ops get secret builder-manager-token     # expect a populated token key
```

### Mechanism resolved, `Dockerfile.engineering` itself still blocked on Secrets RBAC

**The previous documented gap — the builder having no equivalent of
buildx's named additional build context — is closed in source and verified
live 2026-09-29**, but only against a public repo (see the "rootless
BuildKit runs here" bullet above); building this actual Dockerfile is a
separate, still-open item because it additionally needs the
GitHub-App-token/Secrets-RBAC path below, which is not yet live.

`Dockerfile.engineering`'s `COPY --from=shared .agents/agent-manager.md ...`
step (see "Build context" above) depends on a **named additional build
context** — the same primitive `docker buildx build --build-context
shared=<url>` uses. The previous Kaniko-based builder accepted exactly one
`--context` and had no equivalent for a second, separately-authenticated
named context. The current rootless-BuildKit-based builder
(`scripts/builder-manager-mcp.js`) natively supports this via
`buildctl`'s own `--opt context:<name>=<url>` — no buildx wrapper needed,
it's a plain BuildKit dockerfile.v0 frontend feature. Once the Secrets RBAC
below is applied, the call shape will be:

```json
{
  "context_ref": "https://github.com/BuildMy-house/company-os.git#<40-hex-sha>",
  "dockerfile_path": "Dockerfile.engineering",
  "contexts": { "shared": "https://github.com/BuildMy-house/workspace.git#<40-hex-sha>" },
  "image_repo": "company-os-engineering",
  "image_tag": "<tag>"
}
```

The tool is designed to auto-mint and inject the GitHub App token needed
for both private `BuildMy-house/*` repos (`company-os` and `workspace`
share one installation) — see "Named build contexts and git credential
handling" above. Do not pass credentials in either URL yourself.

**Precise current blocker**: this depends on `builder-manager-mcp.js`
creating a per-build Kubernetes Secret to deliver that token to the Job
pod, which needs `create`/`delete` on `secrets` in `company-ops` for the
`builder-manager` ServiceAccount. That RBAC addition
(`k8s/builder-rbac.yaml`, this change) is **not yet applied to the live
cluster** — confirmed via `SelfSubjectAccessReview` (see above). This pod
cannot apply it itself (no `kubectl`, no RBAC-admin permission on any SA it
holds a token for — a pod being unable to grant itself more permission is
the intended security boundary, not a bug to route around). **Minimal
secure next step**: apply the already-narrowly-scoped
`k8s/builder-rbac.yaml` `secrets: ["create", "delete"]` addition to the
live cluster from a host with `kubectl`/cluster-admin access; do not
substitute a broader grant, and do not fall back to Kaniko or a
privileged/root build as a workaround. Once applied, this Dockerfile
(and any other private-repo build) can be exercised end-to-end and this
section updated with real evidence — not before.

### Cross-repo note: canonical agent-manager instructions are stale on this point

The canonical `.agents/agent-manager.md` (in the separate, private
`BuildMy-house/workspace` repo — company-os cannot edit it) states flatly
that in-pod builds are impossible and that OpenCode workers never get a
builder capability. Both statements predate `builder-manager-mcp.js`'s
BuildKit-Job design, which was built specifically to make a safe in-pod
build+push possible without a docker socket, and which is deliberately
**not** on the OpenCode skip-list (see above) because its RBAC is already
narrow enough to be safe there. Flag this to whoever next edits the
canonical file upstream; it is out of scope for this repo to fix.

### When/how to call `builder_build_and_push`

- **Only from an approved, immutable Git revision** — pin `context_ref`
  to a specific commit SHA (`https://github.com/BuildMy-house/company-os.git#<sha>`),
  never a mutable branch name like `#main` or `#prod`. A branch ref can
  move between the moment it's reviewed/approved and the moment BuildKit
  actually clones it; a SHA can't. Resolve the SHA you intend to build
  (`git rev-parse HEAD` on the checkout you just verified) and use that.
- **`dockerfile_path`** is relative to that same context — `Dockerfile`
  for the Hermes image, `Dockerfile.engineering` for this one (pass
  `contexts: { shared: "...#<sha>" }` alongside it — see "Named build
  contexts" above).
- **`contexts`/`build_args`** are optional; every `contexts` value follows
  the same immutable-SHA-pinning rule as `context_ref`.
- **`image_repo`/`image_tag`**: pick something unique and traceable (a
  date or the short SHA you built from), same convention as the manual
  `docker build` path above — there is no fixed `:candidate`/`:latest`
  alias.
- The call blocks until the Job finishes (or `timeout_seconds` elapses)
  and independently re-confirms the tag landed in the registry before
  reporting success — but still treat a "success" response as a claim to
  verify, same as any other worker self-report: check
  `registry_list_tags`/`container_status` yourself before trusting it.

### When/how to call `container_upgrade`

- **Only after** a `builder_build_and_push` (or manual `docker push`) call
  has already independently confirmed the target tag exists in the
  registry — never point a Deployment at a tag you haven't confirmed was
  actually pushed.
- **Check `container_status`/`container_health` before** upgrading, so you
  have a known-good baseline to compare against and, for a self-upgrade,
  the previous-image annotation `container_rollback` depends on.
- **Check `container_health` again after** the rollout — `container_upgrade`
  starts a rollout, it does not wait for it to finish being healthy.
- **If the new image is unhealthy, `container_rollback` immediately** —
  it reverts to the image `container_upgrade` recorded as
  `container-manager/previous-image`, no need to remember the old tag
  yourself.
- For company-os's own self-modification (the `engineering-agent`/
  `engineering-opencode`/`engineering-opencode-direct` Deployments), this
  is still subject to the higher bar in `hermes/SOUL.md`: build to a
  distinctly-tagged candidate, pass `scripts/test-engineering-container.sh`,
  and only then promote — `container_upgrade`/`container_rollback` are the
  mechanism, not a bypass of that discipline.

### `builder-manager` cannot mutate any deployment

`builder-manager`'s RBAC (`k8s/builder-rbac.yaml`) grants
`create`/`get`/`list`/`watch`/`delete` on `jobs.batch`, `get`/`list`/
`watch` on `pods`/`pods/log`, and `create`/`delete` (never `get`/`list`/
`watch`/`patch`) on `secrets` in `company-ops` only — no verbs on
`deployments.apps`, and no ability to read back any Secret including the
ephemeral per-build git-token one it creates itself. The BuildKit Job pod
itself runs with `automountServiceAccountToken: false` (zero Kubernetes
API access of its own). `builder_build_and_push` can
build and push an image; it has no way to point any Deployment at that
image or otherwise change what is currently running — that is exclusively
`container_upgrade`'s job, via the separate `container-manager` MCP server
authenticating as the different `company-ops` ServiceAccount. A
successful build never causes a live change by itself.

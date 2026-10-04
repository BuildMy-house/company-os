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
not a second codebase.

## Manual release control for company-os containers

Pushing or merging code does not update any running company-os container.
This is intentional: a manager controls when each runtime image changes.
For an approved release, build from an immutable commit on `prod` with the
engineering builder MCP's `builder_build_and_push`, test the returned digest
with `container_test`, then use the container-manager MCP's
`container_upgrade` for the specific deployment. Verify readiness and health
after rollout, and use `container_rollback` if it fails. Keep the previous
known-good digest available. Do not use mutable tags or raw Kubernetes
deployment mutations as the routine update path. Workers without the deploy
MCP hand the manager the source SHA, image digest, and test evidence.

This rule applies to the Hive coordinator and every other company-os runtime
deployment, not just the engineering image described below.

## Production deployment

The container runs as the `engineering-agent` Deployment in namespace
`company-ops`. Images come from the in-cluster registry at
`localhost:30500`; updating a Deployment causes kubelet to pull the image.
Check the currently deployed image with:

```bash
kubectl get deployment engineering-agent -n company-ops \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

## Branch discipline

`main` is the integration branch. `prod` is the branch approved for live
images; merging to `prod` requires explicit user approval. Build live images
from an immutable commit on `prod`, never from an unapproved `main` commit.

## Build images through BuildKit

Every ongoing image build uses `builder_build_and_push` from the engineering
manager. It starts a rootless BuildKit Job in k3s outside the agent container;
never run a build inside an agent container or use a host Docker build as the
routine path. For `Dockerfile.engineering`, pin both the company-os source and
the private workspace named context:

```json
{
  "context_ref": "https://github.com/BuildMy-house/company-os.git#<prod-commit-sha>",
  "dockerfile_path": "Dockerfile.engineering",
  "contexts": { "shared": "https://github.com/BuildMy-house/workspace.git#<workspace-commit-sha>" },
  "image_repo": "company-os-engineering",
  "image_tag": "engineering-<source-short-sha>",
  "build_args": { "AGENT_FLAVOR": "claude" }
}
```

For a slim OpenCode image, also set `INSTALL_BROWSER=false` and use the
`company-os-engineering-opencode` image repository. Source refs must be full
commit SHAs with no token embedded in the URL; the builder mints its own
short-lived GitHub App token for private contexts. It verifies the pushed tag
in the registry before returning success.

## Deploy

After confirming the tag exists and checking the current deployment health,
point the Deployment at the new image with the authorized deployment tool or
an administrator's Kubernetes workflow. Wait for rollout readiness, then check
health again. Keep the previous known-good image available for rollback.

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

The engineering manager calls this tool for routine builds. `scripts/builder-manager-mcp.js` gives the engineering-agent a way to trigger
the same kind of build+push
**from inside a k3s pod**, without ever handing that pod a docker socket or
persistent registry credentials — the actual boundary this whole build
pipeline exists to keep:

- The engineering-agent and Hermes build-dispatcher pods never hold a docker
  socket, `k3s ctr` access, `sudo`, or registry push credentials. They can
  only ask the Kubernetes API to create an isolated **Job**.
- `builder_build_and_push(context_ref, dockerfile_path, image_repo,
  image_tag, contexts?, build_args?)` is exposed only to engineering. Hermes
  uses `build_company_os_image(source_sha)` through the separate
  `hermes-build-dispatcher` Service, which hard-codes the company-os repo,
  root Dockerfile, `company-os` image repo, and `hermes-<12-char-sha>` tag.
  It cannot read or edit source, build other repositories, or deploy. The
  Hermes processes receive no GitHub App credentials and the Hermes pod has
  no builder-token mount; the dispatcher runs under the dedicated
  builder-manager ServiceAccount with no workspace checkout. The generic
  call creates a short-lived Job in
  `company-ops` running **two containers** of the rootless `moby/buildkit`
  image (pinned by digest): a `buildkitd` daemon (a native-sidecar
  `initContainer`, Kubernetes >=1.29 — confirmed on this cluster, v1.36) and
  a `buildctl` client (a regular container), talking over a Unix socket on a
  shared `emptyDir` volume — not `buildctl-daemonless.sh`'s single-process
  wrapper. See "Named build contexts and git credential handling" below for
  why they're split. BuildKit builds directly from a
  **git context** (`context_ref`, e.g.
  `https://github.com/BuildMy-house/company-os.git#<40-hex-sha>` — a full
  commit SHA is required, a mutable branch name like `#main` is rejected)
  and pushes straight to `registry.company-ops.svc.cluster.local:5000` — no
  docker daemon involved on either end. It returns the `localhost:30500`
  NodePort image ref for Deployment use because kubelet/containerd cannot
  resolve the in-cluster registry DNS name from the node. Optional
  `contexts: { <name>: <url>
  }` adds **named additional build contexts** (the same primitive
  `docker buildx build --build-context` uses, native to plain BuildKit's
  dockerfile.v0 frontend) — this is what makes `Dockerfile.engineering`
  buildable through this tool; see "Named build contexts" below.
- Verified live in this cluster on 2026-09-29: rootless BuildKit runs here
  with **no privileged workaround** — no `privileged: true`, no added Linux
  capabilities, no `/dev/fuse` hostPath, no `hostUsers`/user-namespaces
  (tried, failed on this node's `newuidmap`/subuid setup, reverted). The
  Job template sets, on the `buildkitd` container specifically,
  `securityContext.seccompProfile: Unconfined`, the
  `container.apparmor.security.beta.kubernetes.io/buildkitd: unconfined` pod
  annotation, a non-root `runAsUser`, and the argv flag
  `--oci-worker-no-process-sandbox` (works around a "mount proc: operation
  not permitted" failure on Dockerfile `RUN` steps under this node's nested
  mount-namespace restrictions) — that combination alone was sufficient for
  full builds to succeed; the `buildctl` client container carries none of
  this relaxation. Confirmed with real Jobs submitted directly against this
  cluster's Jobs API using the `builder-manager` SA token: a baseline
  single-context build with a real `RUN` step, and a named-additional-
  context build resolving two independently-pinned commits of the same
  public repo (asserted via an in-build content diff, not just log
  inspection) — both pushed successfully to the in-cluster registry,
  confirmed independently via a direct `GET /v2/<repo>/tags/list`, with no
  leftover Jobs afterward. All three test fixtures live in this repo
  (`Dockerfile.buildkit-selftest-basic`, `Dockerfile.buildkit-selftest`,
  `Dockerfile.buildkit-selftest-secret-isolation`) and need only
  `company-os`'s own public read access — this decouples verifying the
  BuildKit mechanism itself from the GitHub-App-token/Secrets-RBAC path
  below; the private company-os image build has now exercised that path. See "Secret-isolation
  self-test" below for the exact re-run of these fixtures under the current
  two-container Job design and its result.
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
  mounted at `/var/run/secrets/builder-manager/token` in the
  `engineering-agent` and the separate `hermes-build-dispatcher` workload.
  Hermes itself has no builder-token mount.
- The tool polls the Job to completion, fetches the BuildKit pod's logs on
  either outcome (masking any injected git token before returning them —
  see "Named build contexts" below), deletes the Job (best-effort;
  `ttlSecondsAfterFinished: 600` on the Job spec is the backstop if the
  delete call itself fails), and independently confirms the pushed tag
  actually exists in the registry (`GET /v2/<repo>/tags/list`) before
  reporting success — it does not just trust BuildKit's own exit status.
- Wired into `scripts/generate-agent-mcp-config.js` for engineering runners.
  Hermes instead receives only the separate `build_company_os_image` tool
  from `hermes-build-dispatcher`; it cannot call the generic builder or the
  engineering-manager MCP. The dispatcher enforces a fixed private repo,
  root Dockerfile, image repository, and SHA-derived tag at runtime.


### Named build contexts and git credential handling

`contexts` values (and `context_ref` itself) must be bare, credential-free
`https://` URLs pinned to a full 40-hex commit SHA — enforced by
`builder-manager-mcp.js`'s own validation, not left to caller discipline,
in every version of this design; the URL text itself never carries a
credential.

For the private `BuildMy-house/*` repos specifically (recognized by URL
pattern), the tool mints its own short-lived GitHub App installation token
(reusing `github-app-token.js`, the same mechanism `sync-repo.sh` already
uses) and delivers it to BuildKit as a **pre-flight git-auth build secret**:
`buildctl --secret id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN`. BuildKit's git
source treats a secret with exactly this reserved ID as HTTP(S) git-auth
material for the context fetch itself — consumed internally by `buildkitd`,
entirely out of band from the context URL text, and never written to the
build filesystem unless a Dockerfile explicitly does
`RUN --mount=type=secret,id=GIT_AUTH_TOKEN` (no Dockerfile in this repo
does). The token is delivered to the Job pod via a dedicated, per-build
Kubernetes Secret, referenced with `secretKeyRef` on **only the `buildctl`
container's env** — never the Job manifest's own text, and never
`buildkitd`'s — so it isn't visible to anything that can merely `get`/`list`
Jobs, and (see below) not to a `RUN` step running under this cluster's
relaxed process sandbox either. The Secret is deleted alongside the Job on
every exit path (success, failure, timeout, or a crash before either was
created — an unconditional `try`/`finally` in `buildAndPush`), and also
carries an `ownerReference` to the Job itself as a GC backstop for that
crash case.

Callers should never pass credentials in a `context_ref`/`contexts` value
themselves — the tool's validation rejects any URL containing embedded
credentials, precisely so callers don't need to handle or mint tokens.

**This replaced an earlier design** that built the credentialed URL itself
(`x-access-token:$GIT_TOKEN@github.com/...`) via shell-variable expansion
into a single `buildctl-daemonless.sh` container's command, with the token
as a plain env var on that same container. That had two real, not just
theoretical, exposure paths: (1) BuildKit's own git-source log line echoed
the resolved (credentialed) URL into the pod's stdout logs, visible to
anything with `pods/log` read in `company-ops` for the Job's short
lifetime; and (2) this cluster's required `--oci-worker-no-process-sandbox`
setting (see below) meant a malicious `RUN` step in that same container
could potentially read the token straight out of `/proc/<pid>/environ` of
the very process that held it, regardless of how it got there.

**Job-pod isolation.** `--oci-worker-no-process-sandbox` is required in
this cluster to work around a "mount proc: operation not permitted"
failure on Dockerfile `RUN` steps, but unlike BuildKit's normal fully-
sandboxed `RUN`-step isolation, it does not give a `RUN` step its own
private PID namespace — a `RUN` step can potentially read
`/proc/<pid>/environ` of any other process sharing its container. To close
this off rather than accept it, the Job pod now runs `buildkitd` (which
needs the relaxed sandbox, and is the one running `RUN` steps) and
`buildctl` (the only place `GIT_AUTH_TOKEN` is ever set, and which needs no
sandbox relaxation at all — it only speaks the BuildKit control gRPC
protocol) as **two separate pod containers**, sharing only a Unix socket
over an `emptyDir` volume. Kubernetes gives each container in a pod its own
PID namespace by default (this design deliberately never sets
`shareProcessNamespace: true`), so the secret is never visible to `/proc`
inside the container that actually executes `RUN` steps, no matter how
relaxed that container's own sandbox is.

**Proven, not just designed**, by a malicious-Dockerfile self-test —
`Dockerfile.buildkit-selftest-secret-isolation` — that attempts to read a
hardcoded canary via the default BuildKit secret mount path, its own
process environment, and every readable `/proc/<pid>/environ`, and fails
the build if any of those succeed. See "Secret-isolation self-test" below
for the exact run and result.

### Secret-isolation self-test

Runbook for `Dockerfile.buildkit-selftest-secret-isolation` (see the
fixture's own header comment for the exact leak vectors it checks): submit
a Job directly against this cluster's Jobs API (same two-container
manifest `buildJobManifest()` produces) building that Dockerfile from this
repo's `main` at a pinned commit SHA, with a `GIT_AUTH_TOKEN` env var set
to a **real** freshly-minted GitHub App installation token, on the
`buildctl` container only — a working credential is required for BuildKit's
git source to actually complete the context checkout (a garbage/random
value makes the checkout itself fail before the `RUN` step ever runs,
which proves nothing either way; see below). This test targets the
container-isolation mechanism, not the Secret CRUD path, so it doesn't
need the still-unapplied Secrets RBAC in `k8s/builder-rbac.yaml` — the
token is set as a literal container env value, never a real k8s Secret.
The `RUN` step checks the default BuildKit secret mount path, its own env,
and every readable `/proc/<pid>/environ` for the fixed, public `ghs_`
token-format prefix (not a secret itself, safe to hardcode) rather than a
build-arg-carried marker: **a canary must never be threaded through as a
build ARG/`build-arg`** — BuildKit/Docker injects ARG-declared values into
the RUN step's own process environment regardless of any container
boundary, which makes the self-test "fail" for a reason that has nothing
to do with actual `GIT_AUTH_TOKEN` isolation (an earlier version of this
fixture made exactly this mistake with a build-arg-carried random marker —
see git history — and had to be corrected before its result meant
anything; a follow-up attempt using a real token as both the secret value
and the build-arg value made the identical mistake a second way, since the
value was still threaded through as build-arg). Build succeeds only if the
`ghs_` prefix is found nowhere the `RUN` step can inspect.

- Confirmed live 2026-09-29: submitted directly to this cluster's Jobs API
  using the `builder-manager` SA token, building
  `Dockerfile.buildkit-selftest-secret-isolation` from this repo's `main`
  at the pinned commit that fixed the two ARG false-positives above
  (`1775178207f6ed37c424edd7a0dd7d974218df74`), with a real freshly-minted
  GitHub App installation token set only on the `buildctl` container's env
  (the `buildkitd` container's own env was independently confirmed to
  never contain it, matching `buildJobManifest()`'s output), no build ARG
  involved. **Result: Job succeeded; the `RUN` step's own log line read
  `OK: git-auth secret canary not reachable from this RUN step (checked
  default secret mount, own env, and every readable /proc/*/environ)`** —
  none of the three leak vectors fired. The Job and its pod were both gone
  immediately after completion, confirmed via a direct list against the
  cluster (`leftover jobs: []`, `leftover pods: []`). The same run also
  re-confirmed the two public-build fixtures
  (`Dockerfile.buildkit-selftest-basic`,
  `Dockerfile.buildkit-selftest` with named context pinned at
  `419566f3ade7e69b64e9149a4e739bbf6603c818`) succeed under this same
  two-container manifest, each independently confirmed pushed via
  `GET /v2/<repo>/tags/list`, with no leftover Jobs/pods afterward either.

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

### Deploying the Hermes-scoped build dispatcher

The live `builder-manager` Role now includes only the Job/pod/log and
per-build Secret permissions needed by BuildKit. Hermes does not mount its
token or receive the generic builder tool. Its build-only Service runs in a
separate pod with `automountServiceAccountToken: false`, the dedicated bound
builder token/CA, and only the three GitHub App fields needed to fetch the
fixed `BuildMy-house/company-os` source context. The app key is stripped from
the Hermes startup environment, and Hermes uses the SOUL.md baked into its
image instead of pulling a repository at runtime.

For changes to this boundary, deploy in this order:

1. Promote the reviewed source commit to `prod`.
2. Build and push a new `company-os-engineering` image through the existing
   engineering BuildKit tool, using `Dockerfile.engineering` plus its pinned
   named `workspace` context; update the dispatcher manifest to that image.
3. Build the Hermes image from the same pinned `company-os` commit with the
   engineering BuildKit tool.
4. Apply `k8s/hermes-build-dispatcher.yaml` and `k8s/company-ops.yaml`, then
   roll out `hermes-build-dispatcher` and `hermes-gateway`.
5. Verify the Hermes API tool list contains `build_company_os_image` and does
   not contain `engineering` or `builder_build_and_push`; verify an invalid
   SHA and any extra source/image arguments are rejected without a BuildKit
   Job being created.

### Named-context build of `Dockerfile.engineering`

The builder supports named additional contexts and its private GitHub App
token path. The private company-os image build has been verified; an
end-to-end in-cluster build of this Dockerfile plus its private workspace
context remains a separate verification item.

`Dockerfile.engineering`'s `COPY --from=shared .agents/agent-manager.md ...`
step (see "Build context" above) depends on a **named additional build
context** — the same primitive `docker buildx build --build-context
shared=<url>` uses. The previous Kaniko-based builder accepted exactly one
`--context` and had no equivalent for a second, separately-authenticated
named context. The current rootless-BuildKit-based builder
(`scripts/builder-manager-mcp.js`) natively supports this via
`buildctl`'s own `--opt context:<name>=<url>` — no buildx wrapper needed,
it's a plain BuildKit dockerfile.v0 frontend feature. The engineering-only call shape is:

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

The `builder-manager` Role now grants the required `create`/`delete`
permissions for per-build Secrets. The generic build path is available from
the engineering pod; Hermes cannot request this Dockerfile or the workspace
context through its fixed company-os-only dispatcher.

The canonical agent-manager instructions live in the separate private
`BuildMy-house/workspace` repo. Keep them clear that engineering agents use
the generic BuildKit tool for scoped engineering work, while Hermes receives
only the fixed company-os image builder. Do not grant Hermes the generic
engineering-manager or builder tools.

### When/how engineering should call `builder_build_and_push`

- **Only from an approved, immutable Git revision** — pin `context_ref`
  to a specific commit SHA (`https://github.com/BuildMy-house/company-os.git#<sha>`),
  never a mutable branch name like `#main` or `#prod`. A branch ref can
  move between the moment it's reviewed/approved and the moment BuildKit
  actually clones it; a SHA can't. Resolve the SHA you intend to build
  (`git rev-parse HEAD` on the checkout you just verified) and use that.
- **`dockerfile_path`** is relative to the context. Use the exact Dockerfile
  required by the engineering image and include each immutable named context
  it references.
- **`contexts`/`build_args`** are optional; every `contexts` value follows
  the same immutable-SHA-pinning rule as `context_ref`.
- **`image_repo`/`image_tag`**: use a unique, traceable tag (such as a date
  or the source short SHA); do not overwrite shared `:candidate` or
  `:latest` aliases.
- The call blocks until the Job finishes (or `timeout_seconds` elapses)
  and independently re-confirms the tag landed in the registry before
  reporting success — but still treat a "success" response as a claim to
  verify, same as any other worker self-report: check
  `registry_list_tags`/`container_status` yourself before trusting it.

### When/how to call `container_upgrade`

- **Only after** a `builder_build_and_push` call
  has already independently confirmed the target tag exists in the
  registry — never point a Deployment at a tag you haven't confirmed was
  actually pushed.
- **Check `container_status`/`container_health` before** upgrading, so you
  have a known-good baseline to compare against and, for a self-upgrade,
  the previous-image annotation `container_rollback` depends on.
- `container_upgrade` waits for the requested generation to become ready.
  If it fails its rollout deadline or times out, it restores the previously
  recorded image and waits for that rollback to become ready before returning.
  Always inspect its `rollout` and `rollback` results and call
  `container_health` afterward; if the rollback itself is unhealthy, escalate
  with the recorded previous image.
- The candidate-and-handoff path applies to all agent images: `hermes-gateway`,
  the three engineering Deployments, and `browser-adversary`. Build the
  matching Dockerfile with BuildKit (Hermes through the fixed
  `build_company_os_image` dispatcher; engineering and browser-adversary
  through the engineering-only builder). Test with
  `container_test(image, template_deployment)` using the matching Deployment,
  inspect readiness/logs, and remove the temporary test Deployment. Candidate
  pods retain non-secret settings and readiness probes, strip Secret values,
  Hive registration and Kubernetes/builder credentials, and use empty temporary
  storage instead of production persistent volumes.

  **A running agent must never upgrade its own Deployment.** After saving the
  task checkpoint and evidence, obtain the required Board approval and hand
  the immutable image ref, source SHA, and explicit health/rollback instruction
  to a separate manager with `hermes_ask`. Hermes can upgrade engineering and
  browser-adversary; an engineering manager can upgrade Hermes. That surviving
  manager waits for readiness and can complete automatic rollback after the
  old pod is stopped. All these Deployments use one replica with `Recreate`,
  so expect a short service interruption.

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

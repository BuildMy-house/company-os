## Container operations

## Worker model constraint

For engineering worker dispatches, use only `oc-opencode/mimo-v2.6-flash-free`
or `oc-zai-coding-plan/glm-5.3-flash`. Do not fall back to older MiMo
versions or the tokenrouter GLM free model.

The manager has `container-manager` MCP tools for deployments in
`company-ops`:

- `container_status` / `container_health` / `container_logs` — check
  current state before acting.
- `container_restart` — restart the running container.
- `container_upgrade` — pull and deploy a new image build.
- `container_rollback` — revert to the previous known-good image if an
  upgrade regresses.

Treat these the same as any other risky, hard-to-reverse action: verify
current state first, and prefer `container_rollback` over guesswork if an
upgrade misbehaves.

**Agent image updates:** use this candidate-and-handoff path for
`hermes-gateway`, all three engineering Deployments, and `browser-adversary`.
Build the matching Dockerfile through the appropriate BuildKit tool, verify
the immutable image ref, then call
`container_test(image, template_deployment: "<matching-deployment>")` and
inspect readiness/logs. The test pod keeps non-secret settings/probes,
removes Secret values and Hive/builder/Kubernetes credentials, and replaces
persistent storage with an isolated emptyDir.

The manager doing the rollout must outlive the target. Never call
`container_upgrade` from the pod whose Deployment is being changed: all agent
Deployments use `Recreate`, so that pod can stop before it verifies readiness
or recovers. After the Board has approved deployment, hand the image ref,
source SHA, test evidence, and explicit health/rollback request to a separate
manager. Hermes can upgrade engineering and browser-adversary; an engineering
manager handles Hermes image updates. The surviving manager waits for
readiness and can complete the automatic rollback. A `hermes_ask` handoff is
not deployment approval.

## Dev-cluster gate (buildmyhouse-dev)

For live debugging/verification against the disposable `buildmyhouse-dev`
namespace (auto-deployed copy of `app`'s `dev` branch), see
`docs/DEV-CLUSTER-GATE.md` in the `company-os` checkout. Use the
`kubernetes` MCP tool (same `kubernetes-mcp-server` Hermes runs, see
`hermes/config.yaml`'s `k8s_deployment` entry and
`scripts/generate-agent-mcp-config.js`) for cluster reads/manual rebuild
triggers in that flow -- this image has no `kubectl` binary.

## `builder-manager` — in-pod build+push, never a deploy

If the `builder-manager` MCP server is connected (it is gated on its own
token + script prerequisites — see `docs/DEPLOY-ENGINEERING.md` in the
`company-os` checkout, "Each runner only advertises tools it can launch";
not every runner will have it), `builder_build_and_push(context_ref,
dockerfile_path, image_repo, image_tag, contexts?, build_args?)` triggers
an ephemeral, rootless-BuildKit Job that builds a Dockerfile from a **git
context** and pushes straight to the in-cluster registry — no docker
socket, no `k3s ctr`, no `sudo`, anywhere in this path. The optional
`contexts` map adds named additional build contexts (buildx's
`--build-context` equivalent, native to plain BuildKit) — this is what
closes the previous single-context gap and makes this repo's own
`Dockerfile.engineering` buildable this way (pass `contexts: { shared:
"https://github.com/BuildMy-house/workspace.git#<sha>" }`). Full detail is
in that same `docs/DEPLOY-ENGINEERING.md`. The load-bearing rules to hold
yourself to every time:

- **`context_ref` and every `contexts` value must be an approved,
  immutable Git revision** — a full 40-hex-char commit SHA fragment
  (`https://github.com/<org>/<repo>.git#<sha>`), never a mutable branch
  name; the tool itself rejects anything else. Resolve the SHA from the
  checkout you actually verified (`git rev-parse HEAD`) rather than
  trusting a branch ref not to move between approval and build.
- **Never pass credentials in `context_ref`/`contexts` yourself** — the
  tool rejects URLs with embedded credentials. For private
  `BuildMy-house/*` repos it mints and injects its own short-lived GitHub
  App token via a per-build Kubernetes Secret; you don't need to (and
  can't) do this yourself.
- **`dockerfile_path` is relative to the primary context** — name it
  explicitly (`Dockerfile`, `Dockerfile.engineering`, etc.), don't assume
  a default.
- **`builder-manager` cannot deploy anything.** Its RBAC
  (`k8s/builder-rbac.yaml`) is Jobs/pods-log/its-own-ephemeral-Secret only
  in `company-ops` — no `deployments.apps` verbs at all, and no ability to
  read back any Secret (including its own). The BuildKit Job pod itself
  has no Kubernetes API access whatsoever
  (`automountServiceAccountToken: false`). A successful build+push changes
  nothing about what is currently running. Making a built image live is
  always a separate, explicit `container_upgrade` call via the different
  `container-manager` MCP server (different ServiceAccount entirely).
- **Only call `container_upgrade` after independently confirming the build
  actually landed** (the tool's own response already re-checks the
  registry tag and digest, but verify them yourself too, same as any worker
  self-report), and **check `container_status`/`container_health` before and
  after**. `container_upgrade` waits for readiness and attempts automatic
  rollback on failure; if its rollback result is unhealthy, use
  `container_rollback` with the recorded previous image.
- `scripts/test-engineering-container.sh <image-ref>` is an optional local
  smoke test for an already-built image; it never builds.

## Steward `repo:` names — by checkout, not by convention

`sync-repo.sh` materializes checkouts as `/workspace/<name>-checkout/`.
Pass the matching Steward `repo:` value (with `repo_confirmed: true`) on
your first `lock_file`/`create_work` call for that checkout — don't guess
from the checkout's directory name, since it doesn't always match:

| Checkout                          | Steward `repo:` name  |
|------------------------------------|------------------------|
| `app-checkout/`                    | `buildmy-house-app`   |
| `company-os-checkout/`             | `company-os`          |

`website-checkout/`, `hermees-checkout/`, and `observer-website-checkout/`
have no `AGENTS_STEWARD.md`/`Repo:` identity yet on their source repos —
don't invent a `repo:` name for them; check that checkout's own
`AGENTS_STEWARD.md` first (it may have been added since this was written),
and ask Hermes/the human before locking files there under Steward if it's
still missing.

## Reporting to Hermes

When closing out a wave or a board, report back to Hermes with: tickets
completed/blocked this run, a model/cost breakdown (pulled from
`steward_query_memories` on this repo's `agent-manager/dispatch-log`
scope), any risks or contract gaps surfaced, and next priorities. This is
in addition to, not instead of, the board close-out report described in
the canonical agent-manager instructions' "Completion" section.

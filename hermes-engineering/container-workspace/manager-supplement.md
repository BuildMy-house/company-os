## Container operations

## Worker model constraint

For engineering worker dispatches, use only `oc-opencode/mimo-v2.6-flash-free`
or `oc-zai-coding-plan/glm-5.3-flash`. Do not fall back to older MiMo
versions or the tokenrouter GLM free model.

You also have direct control over this container's own deployment via the
`container-manager` MCP tools — use these when the board/ticket is about
the engineering container itself (self-upgrade, rollback, health):

- `container_status` / `container_health` / `container_logs` — check
  current state before acting.
- `container_restart` — restart the running container.
- `container_upgrade` — pull and deploy a new image build.
- `container_rollback` — revert to the previous known-good image if an
  upgrade regresses.

Treat these the same as any other risky, hard-to-reverse action: verify
current state first, and prefer `container_rollback` over guesswork if an
upgrade misbehaves.

## `builder-manager` — in-pod build+push, never a deploy

If the `builder-manager` MCP server is connected (it is gated on its own
token + script prerequisites — see `docs/DEPLOY-ENGINEERING.md` in the
`company-os` checkout, "Each runner only advertises tools it can launch";
not every runner will have it), `builder_build_and_push(context_ref,
dockerfile_path, image_repo, image_tag)` triggers an ephemeral Kaniko Job
that builds a Dockerfile from a **git context** and pushes straight to the
in-cluster registry — no docker socket, no `k3s ctr`, no `sudo`, anywhere
in this path. Full detail, including the current Kaniko single-context gap
that blocks building this repo's own `Dockerfile.engineering` this way, is
in that same `docs/DEPLOY-ENGINEERING.md`. The load-bearing rules to hold
yourself to every time:

- **`context_ref` must be an approved, immutable Git revision** — a commit
  SHA fragment (`https://github.com/<org>/<repo>.git#<sha>`), never a
  mutable branch name. Resolve the SHA from the checkout you actually
  verified (`git rev-parse HEAD`) rather than trusting a branch ref not to
  move between approval and build.
- **`dockerfile_path` is relative to that same context** — name it
  explicitly (`Dockerfile`, `Dockerfile.engineering`, etc.), don't assume
  a default.
- **`builder-manager` cannot deploy anything.** Its RBAC
  (`k8s/builder-rbac.yaml`) is Jobs/pods-log only in `company-ops` — no
  `deployments.apps` verbs at all, and the Kaniko Job pod itself has no
  Kubernetes API access whatsoever
  (`automountServiceAccountToken: false`). A successful build+push changes
  nothing about what is currently running. Making a built image live is
  always a separate, explicit `container_upgrade` call via the different
  `container-manager` MCP server (different ServiceAccount entirely).
- **Only call `container_upgrade` after independently confirming the build
  actually landed** (the tool's own response already re-checks the
  registry, but verify it yourself too, same as any worker self-report),
  **check `container_status`/`container_health` before and after**, and
  **`container_rollback` immediately if the upgraded deployment comes up
  unhealthy.**
- This container's own self-upgrade (`engineering-agent`/
  `engineering-opencode`/`engineering-opencode-direct`) still needs the
  higher bar above: a distinctly-tagged candidate, a passing
  `scripts/test-engineering-container.sh`, and only then promote — these
  two MCP servers are the mechanism, not a shortcut around that.

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

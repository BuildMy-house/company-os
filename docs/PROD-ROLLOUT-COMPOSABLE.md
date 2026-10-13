# Prod rollout to the composable layout (plan only)

Status: NOT executed. Every step below needs the user's explicit confirmation.
Nothing here is to be run by a planning session; cluster steps go through an
in-cluster agent (hive), not local kubectl.

## Where prod and main stand (checked 2026-10-13)

- `origin/prod` (4416bb1) still has the old flat layout: `k8s/kustomization.yaml`
  plus loose `k8s/*.yaml`. Its tree is identical to the merge-base with main
  (8bd3b53); the only commits prod has that main lacks are three PR merge
  commits (fec7b8f, 6b7dcfe, 4416bb1). No unique prod content.
- `main` is 26+ commits ahead: the base/overlay split (d653221, 75988c1),
  env-driven config (ef03a97, 5d0e043, 16142be), standalone overlays,
  Makefile/CI render gate, plus the other sessions' fixes (7aacfe2, 5a1ba1b,
  1e5f382, ff51f54, feba4c9).
- Rendering `k8s/overlays/buildmy-prod` on main against `origin/prod`
  (`scripts/verify-kustomize-render.sh k8s/overlays/buildmy-prod origin/prod`)
  differs in exactly these lines, all from main's own manifest commits, not the
  restructure:
  - engineering, engineering-opencode, engineering-opencode-direct image:
    `hive-bid-8b29112` -> `hive-complete-1e5f382@sha256:26f13cb2…`
  - engineering-opencode-direct `RUNNER_MODEL`:
    `opencode/mimo-v2.6-flash-free` -> `oc-opencode/mimo-v2.6-flash-free`
  - hive-coordinator image: `company-os-hive:events` ->
    `company-os-hive:reaper-4de8a08@sha256:d7109388…`
- Live images differ from both in two places (docs/IMAGES.md): hermes-gateway
  (live is ahead; sync branch `feat/sync-hermes-gateway-live-image`) and
  engineering-opencode-direct (live behind the repo; rollout pending).

## Pre-checks

1. `git fetch`; confirm main CI is green and `make check` passes locally.
2. `scripts/verify-kustomize-render.sh k8s/overlays/buildmy-prod origin/prod`:
   expect only the five lines above (plus any later main commits; review each).
3. Pull a fresh read-only live image inventory (in-cluster agent) and compare
   with the main render; decide the hermes-gateway sync branch first, otherwise
   applying the render rolls hermes-gateway back.
4. Server-side dry-run via the in-cluster agent:
   `kubectl diff -k k8s/overlays/buildmy-prod` must show only the intended image
   and env changes, no deletions of live objects.
5. Confirm all external consumers below are repointed or ready to be.

## External consumers to repoint

Anything that applies the old flat paths must switch to
`kubectl apply -k k8s/overlays/buildmy-prod` (and `-k k8s/projects/buildmyhouse-dev`
for the dev project):

- In-cluster deploy agents/runbooks that run `kubectl apply -f k8s/<name>.yaml`
  or `-k k8s/`.
- The dev-poller / dev deployer (`scripts/dev-deploy-poller.js`,
  `k8s/projects/buildmyhouse-dev`): the manifest paths moved to
  `k8s/projects/buildmyhouse-dev/`.
- Scripts already updated on main: `scripts/deploy-local.sh`,
  `scripts/k3s-local-up.sh`, `scripts/bootstrap-secrets.sh`; docs updated:
  SECRETS-INFISICAL, REMOTE-MIGRATION, MCP-GATEWAY, DEPLOY-ENGINEERING,
  TELEMETRY. Check any copy of these outside the repo.
- CI: the `kustomize-render-gate` job (new on main) and anything that watches
  `k8s/*.yaml` paths.
- Not in the prod overlay (apply separately as before): `k8s/base/mcp-gateway`,
  `k8s/base/infisical-sync`, `k8s/base/pm-agent-secrets-sync`.

## Ordered steps

1. User confirms this plan and the hermes-gateway decision.
2. Merge the sync branch into main if approved; re-run pre-check 2.
3. Open the usual main -> prod PR (merge, never rewrite prod history).
4. After merge, in-cluster agent runs `kubectl diff -k` then
   `kubectl apply -k k8s/overlays/buildmy-prod`, from the prod checkout.
5. Repoint the consumers above in the same window.
6. Watch rollouts (`kubectl rollout status` per Deployment); hive, hermes-gateway
   and engineering workers are the ones with image changes.
7. Re-pull the live inventory and update docs/IMAGES.md.

## Rollback

- Manifest rollback: revert the prod merge commit and re-apply the old flat
  `k8s/` from the previous prod commit (`kubectl apply -k k8s/` at 4416bb1).
- Workload rollback: `kubectl rollout undo deploy/<name>` for any single
  Deployment; the prior ReplicaSet images are the recorded live refs.
- Resources renamed by the restructure keep their names and namespace, so no
  object is orphaned; the render check proves that, but `kubectl diff` in
  pre-check 4 is the final gate.

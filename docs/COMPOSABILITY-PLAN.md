# Composability plan: deploy a subset of company-os for another project

Goal: stand up e.g. `hive + pm-agent`, or the engineering worker pool, for a
different project from the same manifests and images, with no fork. Evidence
and file:line refs live in [COMPOSABILITY-AUDIT.md](COMPOSABILITY-AUDIT.md).

## Invariant for every ticket

The production render must not change until a ticket explicitly says so:

    KUSTOMIZE=kustomize scripts/verify-kustomize-render.sh k8s/overlays/buildmy-prod <baseline-ref>

must print `RENDER IDENTICAL`. Baseline ref = the commit before COMP-2
(`ff51f54` = origin/main at start). Tickets that intentionally change the
render (COMP-7, COMP-9) re-baseline to the commit before them and say so in
their notes. No cluster applies from planning sessions; rollout goes through
Hive / the in-cluster manager agents (`container_upgrade`).

## Board

Status: done / todo. Deps are ticket IDs. "Files" is the owner set; tickets
sharing a file must be sequenced.

| ID | Ticket | Deps | Files | Status | DoD |
|----|--------|------|-------|--------|-----|
| COMP-0 | Coupling audit | - | docs/COMPOSABILITY-AUDIT.md | done | doc committed; hive/pm-agent claims spot-checked against code |
| COMP-1 | Render-diff verify script | - | scripts/verify-kustomize-render.sh | done | identical vs origin/main; non-zero on a real diff (checked vs 48ae7a5); FORBID_LITERALS exits 2 |
| COMP-2 | Split k8s/ into `base/<component>` + `overlays/buildmy-prod`; delete flat k8s/kustomization.yaml | COMP-1 | k8s/base/**, k8s/overlays/**, path refs in scripts/docs/tests | done | RENDER IDENTICAL (47 docs); pytest 115 pass / 74 skip (test_backup needs boto3, unrelated) |
| COMP-3 | Move remaining loose manifests into base components or a project overlay: `mcp-gateway`, `infisical-sync`, `pm-agent-infisical-sync`, `hermes-data-backup` already moved; `buildmyhouse-dev-*` (4 files) go to `k8s/projects/buildmyhouse-dev/` (its own kustomization, not in base) | COMP-2 | k8s/*.yaml loose files, scripts/dev-deploy-poller.js comments, docs | todo | prod render identical; `ls k8s/*.yaml` empty except examples |
| COMP-4 | Hive: read `ENGINEERING_A2A_URL`, `HIVE_NAME`, `HIVE_URL` from env (runtime.exs), update router test | - | hive/lib/hive/{router,engineering}.ex, hive/config/runtime.exs (new), hive/test | todo | `mix test` green; defaults equal today's literals so render/behaviour unchanged; manifest env var now actually read (currently dead) |
| COMP-5 | pm-agent: `${HIVE_URL}` interpolation in hermes-config.yaml; move refusal text/persona to a per-project content file selected by env | - | pm-agent/hermes-config.yaml, pm-agent/guard.py, pm-agent/SOUL.md, tests/test_pm_agent.py | todo | pytest tests/test_pm_agent.py green; default content identical |
| COMP-6 | Steward URL, GitHub org, registry host, DB name, Axiom dataset become env with current values as defaults: hermes/config.yaml:142, sync-repo.sh, builder-manager-mcp.js (REGISTRY_HOST, org regex, context_ref), init-postgres.sh, both hermes-plugins | - | those files only | todo | grep shows no remaining literal on the code path; existing script tests pass |
| COMP-7 | Image refs as kustomize `images:` entries in the overlay; replace sha/ad-hoc tags (`:events`, `:dns-remediation`, `hive-bid-*`) with a single documented tag scheme (`<component>-<git-sha>`); pin Hermes base image by digest in Dockerfile | COMP-2, COMP-3 | k8s/overlays/buildmy-prod, Dockerfile*, docs/IMAGES.md | todo | render differs ONLY in image lines vs prior baseline, each mapped 1:1 to the images currently running (`kubectl get deploy -o jsonpath` by in-cluster agent) |
| COMP-8 | Namespace/secret-name/registry-host parameterisation: introduce `k8s/overlays/example-standalone` (hive + pm-agent only, namespace `acme-ops`, own registry host, own secret names) using `namespace:` transformer + patches. Replace hardcoded cross-service URLs where they cross namespaces with short names | COMP-2, COMP-4, COMP-5 | k8s/overlays/example-standalone/**, small base edits | todo | `verify-kustomize-render.sh` on buildmy-prod identical; example-standalone render passes `FORBID_LITERALS="buildmyhouse company-ops localhost:30500 homely"` (exit 0) |
| COMP-9 | Engineering worker pool as a standalone component: Dockerfile.engineering `shared` context overridable via build-arg (no hard BuildMy-house URL), repo list from overlay ConfigMap, flavour build-args documented | COMP-6, COMP-8 | Dockerfile.engineering, k8s/base/engineering/**, docs | todo | `docker buildx build --check`; example overlay with engineering component renders clean |
| COMP-10 | Doc + script: `docs/STANDALONE-HIVE-PM.md` ("stand up hive+pm-agent for another project"), `scripts/new-project-overlay.sh <name> <namespace>` scaffold, CI job running verify script on every PR (buildmy-prod identical to baseline file, example-standalone literal-free) | COMP-8 | docs/, scripts/new-project-overlay.sh, .github/workflows/ci.yml | todo | following the doc on a scratch overlay renders and passes the gates |
| COMP-11 | Dev comfort: `make`/script targets `render`, `diff-prod`, `components` (list), kind/k3d local overlay with in-memory hive fallback | COMP-8 | scripts/, Makefile (new) | todo | `scripts/…` works without kubectl (kustomize binary only) |

## Ordering and parallelism

COMP-1, COMP-2 done. Wave A (disjoint files, parallel): COMP-3, COMP-4,
COMP-5, COMP-6. Wave B: COMP-7 (needs COMP-3 landed, and a live-image
inventory from an in-cluster agent), COMP-8. Wave C: COMP-9, COMP-10, COMP-11.
COMP-7 and COMP-9 are the only tickets that may change production behaviour
and need the explicit Hive-mediated rollout; all others keep the render
identical or change only code defaults that equal today's literals.

## Known limits and decisions

- Kustomize forbids an overlay inside a base dir it references (cycle), so
  the flat `k8s/` root was dissolved in COMP-2. Production entry point is now
  `k8s/overlays/buildmy-prod`; any manifest controller that applied `k8s/`
  must be repointed before this merges to prod.
- `main` and `prod` diverged (main 31 ahead, prod 3 ahead at start); prod's
  kustomization had 15 resources, main's 18. Rebase/merge to prod needs the
  human confirmation already required for main->prod.
- Persona content (SOUL.md, AGENTS docs) must be re-authored per project; the
  plan only makes the file selectable, not generic.

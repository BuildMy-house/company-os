# Dev composability targets

Everything here works with the `kustomize` binary alone — **no `kubectl`, no
cluster access, no kubeconfig**. Install kustomize from the release page
(<https://github.com/kubernetes-sigs/kustomize/releases>); v5.4.3 is the
verified version. If it is not on `PATH`, pass the binary explicitly:
`make KUSTOMIZE="/path/to/kustomize" ...`, or fall back to
`make KUSTOMIZE="kubectl kustomize"` where kubectl exists.

Targets (repo root `Makefile`):

- `make components` — list the component/overlay dirs under `k8s/base/`,
  `k8s/projects/`, `k8s/overlays/`.
- `make render [OVERLAY=k8s/overlays/buildmy-prod]` — print a render.
- `make diff-prod [BASE=origin/main]` — render the prod overlay and diff it
  against `origin/main`; the output must stay `RENDER IDENTICAL`. This is the
  safety rail: never change what production deploys without an intentional,
  reviewed change.
- `make gate-example` — `FORBID_LITERALS` gate (no prod literals) on the
  `example-standalone` overlay with `SKIP_BASELINE=1`.
- `make build-all` — build every dir containing a `kustomization.yaml`;
  fails on the first error, prints an OK count.
- `make test` — `python3 -m pytest -q tests --ignore=tests/test_backup.py`.
- `make check` — `build-all` + `diff-prod` + `gate-example` + `test`.

## Local kind/k3d overlay and in-memory hive (design note, not implemented)

Not built as code on purpose:

- Hive is a durable-event coordinator backed by Postgres (leases, work bids,
  event replay). An in-memory fallback would be a code change in `hive/` with a
  different consistency story, not a manifest change, so it needs its own
  design and tests before anyone relies on it.
- `k8s/base/postgres` carries BuildMy secret/DB names (audit item 14), so a
  local overlay that adds it needs its own patches first.

Recommended path when this is picked up: a `k8s/overlays/local` that copies
`example-standalone`, adds a throwaway Postgres (own Deployment, `emptyDir`,
dev password Secret created by the developer), points images at the kind
registry (`localhost:5001`), and is gated by `make gate-example`-style
`FORBID_LITERALS`. Until then, `make render OVERLAY=k8s/overlays/example-standalone`
plus `kind load docker-image` is enough to smoke-test the manifests.

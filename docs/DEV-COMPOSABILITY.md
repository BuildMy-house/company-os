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

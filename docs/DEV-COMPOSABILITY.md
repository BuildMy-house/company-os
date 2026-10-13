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

## Local kind/k3d overlay (`k8s/overlays/local`)

Hive + pm-agent + a throwaway Postgres (emptyDir, trust auth) in namespace
`local-dev`, with neutral names, gated by `make gate-local`.

```bash
kind create cluster                                  # or: k3d cluster create
docker build -f hive/Dockerfile -t localhost:5001/local-hive:dev .
docker build -f pm-agent/Dockerfile -t localhost:5001/local-pm-agent:dev .
kind load docker-image localhost:5001/local-hive:dev localhost:5001/local-pm-agent:dev
kubectl create namespace local-dev
kubectl -n local-dev create secret generic local-secrets --from-literal=PLACEHOLDER=1  # add the keys your build needs
make KUSTOMIZE=kustomize local-up                    # refuses non-kind-*/k3d-* contexts
make local-down
```

Not done, future ticket: an in-memory hive fallback. Hive is a durable-event
coordinator backed by Postgres, so that is a code change in `hive/` with its own
consistency story and tests, not a manifest change. The overlay above avoids it
by giving hive a disposable real database.

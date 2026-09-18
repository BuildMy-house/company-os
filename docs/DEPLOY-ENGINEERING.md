# Engineering container build / deploy / rollback

## What the engineering container is

The `engineering-agent` runs the Claude engineering-manager persona plus
opencode/codex CLI workers for Hermees's own dispatch path. It is defined by
`Dockerfile.engineering` at the repo root and started by
`scripts/engineering-entrypoint.sh`, which syncs BuildMy-house repos then
execs `supergateway` serving `ai-cli-mcp`.

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

The Deployment's container image is `docker.io/library/company-os-engineering`,
built locally (not pulled from a registry) and imported directly into the
k3s node's containerd image store.

There is **no fixed tag convention** (no `:candidate`/`:latest`/`:previous`
alias enforced anywhere). A redeploy means building a new, uniquely-named
tag and pointing the Deployment at it — not overwriting a shared tag. Check
what tag is currently live at any time with:

```bash
kubectl get deployment engineering-agent -n company-ops \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

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
  -t company-os-engineering:<tag> .
```

Pick `<tag>` as something unique and traceable (e.g. a date or short git
SHA) — it does not need to follow any reserved name.

## Import the image into k3s

k3s's containerd store is separate from the normal Docker daemon's image
store, so the built image has to be explicitly imported. `k3s ctr` requires
`sudo` since it operates outside the user's normal docker permissions:

```bash
docker save company-os-engineering:<tag> | sudo k3s ctr images import -
```

## Deploy

Point the running Deployment at the newly-imported tag:

```bash
kubectl set image deployment/engineering-agent \
  engineering-agent=docker.io/library/company-os-engineering:<tag> \
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
   node's local containerd image store:

   ```bash
   sudo k3s ctr images list | grep company-os-engineering
   kubectl set image deployment/engineering-agent \
     engineering-agent=docker.io/library/company-os-engineering:<older-tag> \
     -n company-ops
   kubectl rollout status deployment/engineering-agent -n company-ops
   ```

## Image cleanup

Old/unused image tags accumulate in the k3s node's containerd store over
time since builds are never automatically pruned. Operators should
periodically check for tags no longer referenced by any Deployment and
remove them:

```bash
sudo k3s ctr images list | grep company-os-engineering
sudo k3s ctr images rm <unused-tag>
```

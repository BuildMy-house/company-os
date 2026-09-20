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

## Agent-triggered build+push: `builder-manager` (Kaniko Job), no docker socket anywhere

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
  image_tag)` creates a short-lived Job running
  `gcr.io/kaniko-project/executor` in `company-ops`. Kaniko builds directly
  from a **git context** (`context_ref`, e.g.
  `https://github.com/BuildMy-house/company-os.git#main`) and pushes
  straight to `registry.company-ops.svc.cluster.local:5000` — no docker
  daemon involved on either end.
- The Job pod itself runs with `automountServiceAccountToken: false` — it
  has zero Kubernetes API access; it only ever talks to the git remote and
  the registry over plain HTTP/HTTPS.
- The MCP tool (not the Job) authenticates to the Kubernetes API as a
  **dedicated `builder-manager` ServiceAccount** (`k8s/builder-rbac.yaml`),
  deliberately separate from the shared `company-ops` SA that
  `container_manager`/`k8s_deployment` use. Its Role can only
  `create`/`get`/`list`/`watch`/`delete` **Jobs** and read their pods' logs
  — it cannot touch Deployments, Secrets, or anything else. This SA's
  bound token is mounted at a distinct path
  (`/var/run/secrets/builder-manager/token`, via a Secret + volume added to
  both the `hermes-gateway` and `engineering-agent` Deployments), separate
  from the pod's default in-cluster SA token path.
- The tool polls the Job to completion, fetches the Kaniko pod's logs on
  either outcome, deletes the Job (best-effort; `ttlSecondsAfterFinished:
  600` on the Job spec is the backstop if the delete call itself fails),
  and independently confirms the pushed tag actually exists in the
  registry (`GET /v2/<repo>/tags/list`) before reporting success — it does
  not just trust Kaniko's own exit status.
- Wired into `scripts/generate-agent-mcp-config.js` for both Hermes and
  Claude's engineering-manager (same `fs.existsSync(serviceaccount token)`
  gate as `container-manager`/`registry-manager`), and registered directly
  in `hermes/config.yaml` as `builder_manager`. Unlike
  `container-manager`/`registry-manager`/`hermes-messenger`, it is **not**
  added to the OpenCode skip-list in `generate-agent-mcp-config.js` — its
  own RBAC is already narrow enough that OpenCode workers dispatched from
  engineering-manager can safely trigger builds too.

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

This has not been applied or live-tested as of this doc's last edit — see
the repo's Steward task history / the agent-manager session report for the
current status. Verify before trusting `builder_build_and_push` to work:

```bash
kubectl -n company-ops auth can-i create jobs.batch \
  --as=system:serviceaccount:company-ops:builder-manager   # expect yes
kubectl -n company-ops get secret builder-manager-token     # expect a populated token key
```

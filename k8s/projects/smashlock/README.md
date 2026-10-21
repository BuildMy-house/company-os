# SmashLock k8s Project (Cluster 1)

Static Kubernetes manifests for running SmashLock Hive workers on cluster 1
(`company-ops` namespace). **Nothing is applied automatically** — this directory
is applied on demand via `builder-gw` / `container-manager-gw` as a separate
human-approved step.

## Resources

| File | Purpose |
|------|---------|
| `smashlock-opencode.yaml` | Cheap-tier opencode worker (`AGENT_ROLE=worker`, `AGENT_TIER=cheap`, `AGENT_REPOS=smashlock`) |
| `smashlock-reviewer.yaml` | Reviewer pool worker (`AGENT_CAPABILITIES=review`, replicas=1) |
| `smashlock-infisical-sync.yaml` | InfisicalStaticSecret syncs for `smashlock-secrets` and Steward identity |

All Deployments reuse the existing `company-ops` ServiceAccount. No additional
RBAC is required.

## Infisical `/smashlock` Folder (dev environment)

A human must create the following keys in the Infisical folder `/smashlock`
(project "Build My house", environment `dev`) before applying:

| Key | Description |
|-----|-------------|
| `GITHUB_TOKEN` | Fine-grained PAT scoped to **exactly** `BuildMy-house/SmashLock` with `contents:write` + `pull_requests:write` |
| `AXIOM_API_KEY` | Axiom API key for `smashlock-opencode-k8s` service |
| `AXIOM_DATASET` | Axiom dataset name (e.g. `smashlock-logs`) |
| `TOKENROUTER_API_KEY` | TokenRouter API key for `oc-tokenrouter/z-ai/glm-5.3-free` model |

The folder must contain **exactly** these keys — no more, no less. The
InfisicalStaticSecret `smashlock-sync` copies the entire folder into the
Kubernetes Secret `smashlock-secrets` in `company-ops`.

A second InfisicalStaticSecret (`company-ops-steward-smashlock-opencode`)
syncs the Steward identity (keys `STEWARD_MCP_URL` and `STEWARD_TOKEN`) from
`/infra/steward/smashlock-opencode` into its own Secret `company-ops-steward-smashlock-opencode`, mirroring the pattern for
`engineering-opencode`.

## Steward Repo Identity

Work submitted to Hive for SmashLock carries `repo: "smashlock"` in the work
payload. Workers declare `capabilities.repos: ["smashlock"]` at registration
(via `AGENT_REPOS=smashlock` env var). This enables repo-scoped bidding per
the `requires_tools` / `capabilities.tools` mechanism.

## Remote Node Registration (Future)

When a remote node is added for SmashLock, `registries.yaml` must include an
HTTP endpoint at `100.105.155.86:30500`. Workers on that node **must register
tailnet endpoints** (e.g. `http://smashlock-opencode.tailnet:8001`), not
in-cluster DNS names, because the remote node cannot resolve cluster-internal
DNS.

## Validation

```bash
# Kustomize build (no cluster contact)
kubectl kustomize k8s/projects/smashlock >/dev/null

# Verify overlay still works
kubectl kustomize k8s/overlays/buildmy-prod >/dev/null

# YAML parse check for each file
python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" k8s/projects/smashlock/smashlock-opencode.yaml
python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" k8s/projects/smashlock/smashlock-reviewer.yaml
python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1])))" k8s/projects/smashlock/smashlock-infisical-sync.yaml
```

## Applying

```bash
# Human-approved, via builder-gw / container-manager-gw
kubectl apply -k k8s/projects/smashlock
```

## Open items before first apply

- The pinned image `company-os-engineering:ticket-std-714c938` predates the
  `SmashLock` case added to `scripts/sync-repo.sh`; rebuild the image (via
  builder-gw, human-approved) or workers cannot `sync-repo.sh SmashLock`.
- Steward tenant: SmashLock's own docs currently use `Repo: fucklock` on
  `anantha.stewardacs.xyz`; the worker identity here points at whatever
  `STEWARD_MCP_URL` is stored in `/infra/steward/smashlock-opencode`. Decide
  the tenant and repo name before creating that secret.

# Engineering container build / promote / rollback

## What the engineering container is

The `engineering-agent` Deployment (`k8s/engineering.yaml`, local k3s —
the old `docker-compose.yml` `engineering` service was retired once k3s
became the sole authoritative deployment) runs the Claude
engineering-manager persona plus opencode/codex CLI workers for
Hermees's own dispatch path. It is defined by
`Dockerfile.engineering` and started by
`scripts/engineering-entrypoint.sh`, which syncs five BuildMy-house
repos then execs `mcp-proxy` serving `ai-cli-mcp`.

## Image tag convention

| Tag | Purpose |
|---|---|
| `engineering:candidate` | Fresh build, not yet verified |
| `engineering:latest` / `company-os-engineering:container-manager` | The live image running in the k3s `engineering-agent` Deployment |
| `engineering:previous` | Last-known-good, kept for rollback |
| `engineering:selftest` | Used by the self-test script only |

`k8s/engineering.yaml` references the image by tag
(`docker.io/library/company-os-engineering:container-manager`) with
`imagePullPolicy: Never` — a new build must be imported into k3s's
containerd (`docker save ... | sudo k3s ctr images import -`, or
`scripts/deploy-local.sh`) and the Deployment rolled before a tag change
takes effect; retagging alone does not repull.

## Build (manual)

From the repo root:

```bash
docker buildx build \
  -f Dockerfile.engineering \
  -t engineering:candidate \
  .
```

Or from the repository root:

```bash
cd company-os
docker buildx build \
  -f Dockerfile.engineering \
  -t engineering:candidate \
  .
```

## Verify

```bash
company-ops/scripts/test-engineering-container.sh
```

This builds `engineering:selftest` and runs the full checklist. All
checks must PASS (WARN for known access gaps is acceptable).

## Promote

Only after the self-test passes:

```bash
# Preserve current live as previous for rollback
docker tag engineering:latest engineering:previous 2>/dev/null || true

# Promote candidate to live, tagged as the k3s manifest expects
docker tag engineering:candidate docker.io/library/company-os-engineering:container-manager

# Import into k3s and roll the deployment
docker save docker.io/library/company-os-engineering:container-manager | sudo k3s ctr images import -
kubectl -n company-ops rollout restart deploy/engineering-agent
kubectl -n company-ops rollout status deploy/engineering-agent --timeout=180s
```

If no prior `:latest` exists (first deployment), skip the
`:previous` tagging step.

## Rollback

If the promoted image has problems:

```bash
docker tag engineering:previous docker.io/library/company-os-engineering:container-manager
docker save docker.io/library/company-os-engineering:container-manager | sudo k3s ctr images import -
kubectl -n company-ops rollout restart deploy/engineering-agent
kubectl -n company-ops rollout status deploy/engineering-agent --timeout=180s
```

## Notes

This is a **manual procedure** for now. Full automation into a single
promote/rollback script would be reasonable future work but is
deliberately not built until the container has shipped more than a
handful of changes.

# Engineering container build / promote / rollback

## What the engineering container is

The `engineering` service in `docker-compose.yml` runs the Claude
engineering-manager persona plus opencode/codex CLI workers for
Hermees's own dispatch path. It is defined by
`Dockerfile.engineering` and started by
`scripts/engineering-entrypoint.sh`, which syncs five BuildMy-house
repos then execs `supergateway` serving `ai-cli-mcp`.

## Image tag convention

| Tag | Purpose |
|---|---|
| `engineering:candidate` | Fresh build, not yet verified |
| `engineering:latest` | The live image running in compose |
| `engineering:previous` | Last-known-good, kept for rollback |
| `engineering:selftest` | Used by the self-test script only |

The `engineering` service in `docker-compose.yml` currently uses an
inline `build:` block with no explicit `image:` tag. To introduce a
tagged promotion flow, build with an explicit tag and update the compose
service to reference it (or use `docker tag` after build and restart).

## Build (manual)

From the repo root:

```bash
docker buildx build \
  -f company-ops/Dockerfile.engineering \
  --build-context manager-def=.claude/agents \
  --build-context skills-src=.agents/skills \
  -t engineering:candidate \
  company-ops
```

Or from `company-ops/`:

```bash
cd company-ops
docker buildx build \
  -f Dockerfile.engineering \
  --build-context manager-def=../.claude/agents \
  --build-context skills-src=../.agents/skills \
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

# Promote candidate to live
docker tag engineering:candidate engineering:latest

# Restart the compose service
cd company-ops
docker compose up -d engineering
```

If no prior `:latest` exists (first deployment), skip the
`:previous` tagging step.

## Rollback

If the promoted image has problems:

```bash
docker tag engineering:previous engineering:latest

cd company-ops
docker compose up -d engineering
```

## Notes

This is a **manual procedure** for now. Full automation into a single
promote/rollback script would be reasonable future work but is
deliberately not built until the container has shipped more than a
handful of changes.

# Image references

## Scheme

Immutable refs only for anything that runs in production:
`localhost:30500/<image>:<component>-<git-sha>@sha256:<digest>` (tag for humans,
digest for the cluster). Mutable tags (`:latest`) are tolerated only for
`company-os-browser-adversary` and `company-os-pm-agent` until their build path
emits a sha tag. Base images are pinned by digest (`Dockerfile`: Hermes base).

## Why there are no `images:` entries in `buildmy-prod`

The kustomize images transformer matches by image *name* and rewrites every
occurrence. `company-os-engineering` and `company-os` each run with several
different refs in production (tag vs digest, per workload), so one entry would
change the render. COMP-7 therefore keeps the manifest refs as the source of
truth and the `buildmy-prod` render byte-identical; neutral overrides live in the
example overlays (`images:` entries there).

## Repo vs live drift (live inventory, ns company-ops, 2026-10-13, read-only)

Everything not listed matches the repo (hive, engineering-agent,
engineering-opencode, both build dispatchers, mcp-gateway, pm-agent,
browser-adversary, dns-healthcheck, backup cronjobs, nginx, postgres, registry).

| Workload | Repo | Live |
|---|---|---|
| engineering-opencode-direct | `company-os-engineering:hive-bid-8b29112` | `company-os-engineering:engineering-6b7dcfe5244d@sha256:4762d2c23118e491bf2d0de3191d6c32e9fc59faeb2354442640900b6ae7a873` |
| hermes-gateway | `company-os@sha256:89b92c0be5cc05aa23d10043c89a5dcb3de6a925783f33321952985d8d761b4b` | `company-os:hermes-6b7dcfe5244d@sha256:e9c3462c4ee90cf33c6b62e05fea3fa92fdc3d76c6e41185d5a27f268b4263a2` |

Not reconciled automatically: deciding whether the repo or the cluster is right
is a deploy decision.

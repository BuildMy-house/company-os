# Hermes Operations

Multi-domain operations structure for the Hermes AI engineering team.
Shared infrastructure with domain-specific overlays for engineering,
marketing, design, and future domains.

## Structure

```
hermes-operations/
  shared/                       # Common rules, graphify, scripts
  domains/
    engineering/                # Software development
    marketing/                  # Content, SEO, campaigns
    design/                     # UI/UX, brand, accessibility
  HOW-TO-ADD-A-DOMAIN.md        # Guide for adding new domains
```

## Quick Start

Each domain has its own `opencode.json` that extends the shared base.
To work in a domain:

```bash
cd domains/engineering
# OpenCode will load shared/AGENTS.base.md + domain AGENTS.md + AGENTS_STEWARD.md
```

## Adding a Domain

See `HOW-TO-ADD-A-DOMAIN.md` for step-by-step instructions.

## Deployment

The `engineering-agent` container clones this repo and loads all domains. The
entrypoint pulls the latest on every restart. It runs only as a Kubernetes
Deployment (no Docker Compose path) — see `docs/DEPLOY-ENGINEERING.md` at the
repo root for the build/push/deploy flow.

## Key Files

| File | Purpose |
|------|---------|
| `shared/AGENTS.base.md` | Universal rules for all agents |
| `shared/opencode.base.json` | Base OpenCode config template |
| `shared/MODEL_POLICY.md` | Model selection and cost policy |
| `shared/.opencode/` | Shared graphify skill/plugin/command |
| `shared/scripts/verify-all.sh` | Local CI gate |
| `domains/<name>/AGENTS.md` | Domain-specific rules |
| `domains/<name>/AGENTS_STEWARD.md` | Domain coordination protocol |
| `domains/<name>/PLAN.md` | Domain claim board |
| `domains/<name>/opencode.json` | Domain OpenCode config |
| `domains/<name>/knowledge/` | Accumulated domain knowledge |

## Steward Integration

Each domain uses Steward scope path: `company-os/<domain-name>`
Scoping is automatic — no manual configuration needed.

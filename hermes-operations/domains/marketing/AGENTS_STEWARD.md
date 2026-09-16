# Marketing — Steward Coordination

Extends `shared/AGENTS.base.md` Steward section. Read that first.

## Repo Identity

- **Repository**: `company-os`
- **Branch**: `main` (shared checkout)
- **Project**: Homely marketing — content, campaigns, SEO, brand

## Layout & Ownership (STRICT)

| Directory | Owner | Notes |
|---|---|---|
| `docs/marketing/` | marketing-dev | Campaign briefs, content calendar |
| `docs/brand/` | marketing-dev | Brand guide, voice, visual identity |
| `website/src/content/` | marketing-dev | Blog posts, landing page copy |
| `PLAN.md` | marketing-manager | Live claim board |

**Rule**: Never edit outside your owner directories.

## Coordination Protocol

### 1. PLAN.md is the live claim board

Read it first. Claim by editing only your row and committing:
```
board: claim <TICKET-ID>
```

### 2. Status flow

```
todo → claimed → in_progress → review → done
```

- **Only the manager sets `done`**
- Workers cannot self-approve
- `blocked`: set status + write reason in Notes column

### 3. Steward lifecycle per ticket

```
claim_work(task_id)
  → lock_file every file before editing
    → implement
      → verify (run DoD commands, capture output)
        → save_memory / skill_save (record learnings)
          → unlock files
            → commit (stage only your files)
              → update PLAN.md row
                → release_work
                  → submit_task_feedback (LAST)
```

## DoD Checklist (per ticket type)

### Blog post
- [ ] Word count meets target (specified in ticket)
- [ ] Primary keyword in title, H1, first 100 words
- [ ] Meta description ≤ 160 chars
- [ ] Readability score ≥ 60 (Flesch-Kincaid)
- [ ] No brand voice violations
- [ ] Internal links to ≥ 2 related pages

### Landing page
- [ ] Headline matches value proposition
- [ ] CTA above the fold
- [ ] Load time < 3s
- [ ] Mobile responsive
- [ ] SEO meta tags complete

### Social copy
- [ ] Character count within platform limits
- [ ] Hashtag count appropriate (3-5 for LinkedIn, 1-2 for Twitter)
- [ ] CTA included

## Steward Scope

Domain scope path: `company-os/marketing`

# Nahar TODO — items only Nahar can resolve

This file tracks blocked/deferred items across the Hermees V0 workstream that
require real credentials, an account, or a policy decision only Nahar can
make. See `company-ops/PLAN.md`'s "Credential boundary" note and
`/home/nahar/.claude/plans/ticklish-conjuring-horizon.md` for full context.
Referenced from `company-ops/.env.example` and `company-ops/hermes/config.yaml`
— do not remove this file, append new items instead.

## Open

### 1. `DISCORD_DM_USER` unset in production `.env` (found 2026-09-06, E2E pass)
The real `company-ops/.env` has no `DISCORD_DM_USER` set at all. This means
the Human Interface (`company_ops/human_interface.py`'s
`ask_information`/`ask_judgment`/`request_approval`/`request_action`) has no
way to actually deliver a DM to Nahar right now — there's no destination user
ID configured. **Needs: your real Discord user ID** (right-click your name in
Discord with Developer Mode on → "Copy User ID"), set as `DISCORD_DM_USER` in
the real `.env`.

### 2. `DISCORD_ALLOW_ALL_USERS=true` with no allow-list configured (found 2026-09-06, E2E pass)
The real `company-ops/.env` has `DISCORD_ALLOW_ALL_USERS=true` and no
`DISCORD_ALLOWED_USERS` set. This makes the P2-F DM allow-list security fix
(commit `54d89a5`, gating DMs on `is_user_allowed()`) a **no-op in
production** — any Discord user who can DM the bot or @mention it in an
allowed channel can currently trigger a real (non-dry-run) worker dispatch.
**Needs a decision:** either set `DISCORD_ALLOW_ALL_USERS=false` and populate
`DISCORD_ALLOWED_USERS` with your real Discord user ID(s) (and
`DISCORD_ALLOWED_CHANNELS` if channel-scoping is also wanted), or an explicit,
deliberate call from you to keep it open (not recommended given item 5 below
— this is exactly why the `hermes gateway run` container stays off this
session). Not invented or changed by any agent — this is your policy call.

### 3. Neon Postgres (production) not provisioned yet
`COMPANY_DATABASE_URL` / `OBSERVER_DATABASE_URL` / `ANALYTICS_DATABASE_URL`
are unset in the real `.env`. Phase 1's Postgres backbone is fully built and
tested against local/dev Postgres only (`company-ops/docker-compose.test.yml`
+ `company-ops/scripts/test-db-up.sh`); nothing is live. **Needs:** a real
Neon project, then run `company-ops/sql/company_schema.sql`,
`company-ops/sql/observer_schema.sql`, and `company-ops/sql/roles.sql`
against it (same order `test-db-up.sh` uses), then set the three connection
strings (with real per-role passwords, not the `localtest_*` dev ones) in the
real `.env`.

### 4. pgEdge account/API key not provisioned yet
`PGEDGE_API_KEY` is unset. `company-ops/hermes/config.yaml`'s
`pgedge_analytics` MCP stanza is wired but inactive until this exists.
**Also unverified:** the npm package name `@pgedge/mcp-server` referenced in
that stanza — confirm the real published package name before going live (the
config file's own comment already flags this).

### 5. `hermes gateway run` (the production vendor container) still not started
`company-ops/docker-compose.yml`'s `company-ops` service (`command: ["hermes",
"gateway", "run"]`) has not been started by any agent session, including this
one — explicitly left alone per your standing instruction (real credential
spend, unbounded autonomous duration). Given items 1 and 2 above, starting it
now would mean Hermees has no way to actually reach you (item 1) and the
Discord bridge would accept real dispatch triggers from any Discord user
(item 2). Resolve 1 and 2 first, then this is your call on when to flip on.

### 6. site-homely deployment is broken/non-live (confirmed 2026-09-06)
`site-homely` is its own independent git repo
(`github.com/NaharEmet/homely-site.git`, not a submodule of this repo).
Confirmed by direct check this session:
- Its "Deploy to Cloudflare Pages" GitHub Actions workflow has failed on all
  3 of its runs to date.
- GitHub Pages is not even enabled for that repo (`gh api
  repos/NaharEmet/homely-site/pages` → 404).
- Local `wrangler whoami` shows no Cloudflare account authenticated at all.
- There's an inherent target mismatch: `astro.config.mjs` uses the
  `@astrojs/cloudflare` adapter (built for Cloudflare Workers), but the CI
  workflow actually deploys the static output to **GitHub Pages** via
  `actions/deploy-pages` — two different hosting targets, neither currently
  live.
**Needs a decision + credentials:** pick one target (Cloudflare
Workers/Pages, matching the adapter already in use, is the natural choice
given Phase 9's staged-deployment requirement) and supply a
`CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` as repo secrets on
`homely-site` (or, if GitHub Pages is preferred instead, enable Pages in that
repo's Settings → Pages, and drop the Cloudflare adapter). Ticket P9-F (this
run) documents the gradual/staged-deployment procedure for whichever target
you choose, but cannot go live without this.

## Resolved this session (2026-09-06)

### GitHub remote for `house_designer`/`homely` — was already fine, just unpushed
`git remote -v` already had `origin` → `github.com/NaharEmet/homely.git`, and
`gh auth status` was already authenticated (account `NaharEmet`, scopes
include `repo`+`workflow`). The actual gap was that the local branch
(`fix/manager-driven-20260828`, which is also this repo's configured GitHub
default branch) was **138 commits ahead of origin, never pushed** — so CI had
never run against any of the Phase 1-2 work. Pushed this session; `ci.yml` is
now running for real on push. No action needed from you here.

# Nahar TODO — items only Nahar can resolve

This file tracks blocked/deferred items across the Hermees V0 workstream that
require real credentials, an account, or a policy decision only Nahar can
make. See `company-ops/PLAN.md`'s "Credential boundary" note and
`/home/nahar/.claude/plans/ticklish-conjuring-horizon.md` for full context.
Referenced from `company-ops/.env.example` and `company-ops/hermes/config.yaml`
— do not remove this file, append new items instead.

**Reorganized 2026-09-06** into a dependency-ordered checklist (previously a
flat numbered list) so the blocking chain is visible at a glance. All
original content/detail preserved; only the grouping changed, plus item 7's
claim corrected (see that item for what changed and why).

## Open

### Group A — blocks the Discord bridge being safe to run unattended

Both items below must be resolved before the bridge (and therefore the
`hermes gateway run` container in Group C) can safely accept real Discord
input.

#### A1. `DISCORD_DM_USER` (superseded for Human Interface 2026-09-07, P2-J)
Human Interface outbound delivery (`company_ops/human_interface.py`'s
`ask_information`/`ask_judgment`/`request_approval`/`request_action`) was
switched to posting in the server `#hil` channel via `DISCORD_HIL_CHANNEL`
(`1546470198825975961`), per Nahar's request that communication stay on the
server. `DISCORD_DM_USER` is no longer read by `human_interface.py`. It remains
set in `.env` (`324293400851382273`) and is only used by legacy `discord_bridge.py`
DM notification flows (`maybe_ask_questions`, daily update).


#### A2. ~~`DISCORD_ALLOW_ALL_USERS=true` with no allow-list configured~~ — RESOLVED (confirmed 2026-09-07)
Found 2026-09-06 (E2E pass). Re-checked live 2026-09-07: production `.env`
now has `DISCORD_ALLOW_ALL_USERS=false` and `DISCORD_ALLOWED_USERS=324293400851382273`
set — the P2-F DM allow-list fix (commit `54d89a5`) is live and enforced, not
a no-op. No further action needed on this item.

**New, separate finding (2026-09-07):** `GATEWAY_ALLOW_ALL_USERS=true` is
also set in `.env` — this is a different, vendor-level (`hermes gateway`)
flag, not the Discord-specific one above. Traced it to only affect the
WhatsApp/Yuanbao platform adapters (`grep` inside the running container
shows it referenced in `whatsapp_common.py`/`whatsapp_cloud.py`/`yuanbao.py`
only). No WhatsApp/Telegram/Yuanbao credentials exist in `.env`, so this
flag is currently inert — not a live gap today, but worth setting to `false`
explicitly (or scoping it) before any of those platforms are ever wired up,
so it isn't accidentally wide-open the moment a new platform's credentials
are added.

### Group B — blocks real data/analytics

Independent of Group A; both items below are separately needed before Hermes
has any real production data to reason about.

#### B1. ~~Production Postgres not stood up yet~~ — RESOLVED (2026-09-07)
Self-hosted Postgres 16 container added to `company-ops/docker-compose.yml`
as the `postgres` service (no ports exposed — internal-network-only). First
run applies `company_schema.sql`, `observer_schema.sql`, `roles.sql`, then
sets real passwords via `04-set-role-passwords.sh`. Schemas, roles, and
permission boundaries verified against the live container. Connection strings
with real per-role passwords are in the production `.env`.

#### B2. ~~pgEdge account/API key not provisioned yet~~ — RESOLVED (2026-09-07)
pgEdge approach abandoned entirely. `PGEDGE_API_KEY` removed from
`.env.example`. `config.yaml`'s `pgedge_analytics` stanza replaced with
`postgres_analytics` using `@microsoft/postgres-mcp` (latest `0.1.0-rc.10`,
Node 22+). MCP server configured to connect directly to the production
self-hosted Postgres via `ANALYTICS_DATABASE_URL`.

### Group C — blocks the actual `hermes gateway run` container starting

Depends on Group A and Group B both being resolved first (Hermes needs a way
to reach you AND a real database before it should run unattended).

#### C1. `hermes gateway run` (the production vendor container) still not started
`company-ops/docker-compose.yml`'s `company-ops` service (`command: ["hermes",
"gateway", "run"]`) has not been started by any agent session, including this
one — explicitly left alone per your standing instruction (real credential
spend, unbounded autonomous duration). Given Group A items above, starting it
now would mean Hermees has no way to actually reach you (A1) and the
Discord bridge would accept real dispatch triggers from any Discord user
(A2). Given Group B, it would also have no real database to operate against.
Resolve A and B first, then this is your call on when to flip on.

### Group D — separate/independent (not on the Group A→B→C chain)

#### D1. site-homely deployment is broken/non-live (confirmed 2026-09-06)
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
repo's Settings → Pages, and drop the Cloudflare adapter). Ticket P9-F
(in_progress on the company-ops board) documents the gradual/staged-
deployment procedure for whichever target you choose, but cannot go live
without this.

#### D2. Steward MCP token — sensitive, but NOT publicly leaked (corrected 2026-09-06)
**Correction to a prior claim in this file:** this item previously stated
that the Steward bearer token in `.mcp.json` "was already pushed to the
public homely GitHub repo." **That claim was FALSE and has been corrected.**
Independently re-verified 2026-09-06 via three separate checks against this
repo's actual history: `git ls-files .mcp.json` returns nothing (the file
has never been tracked), `git check-ignore -v .mcp.json` confirms it matches
`.gitignore`'s `/.mcp.json` rule, and `git log --all -- .mcp.json` returns
zero commits ever. `.mcp.json` exists only as a local, gitignored file on
this machine — it was never committed and never pushed anywhere.

The token should still be treated as sensitive, just not on an
"already-public" urgency basis: multiple AI sessions have read its literal
contents this week (visible directly in the file, not reproduced here on
purpose), and any process/agent that read it could in principle retain or
leak it independent of git history. Separately, two untracked files —
`company-ops/opencode/opencode.json` and
`company-ops/opencode/worker/opencode.json` — contain a DIFFERENT token
value for the same endpoint (`https://homely.stewardacs.xyz/mcp/coding/sse`),
also viewable directly in those files, also never committed.
**Needs:** (a) your call on whether to rotate either or both tokens at
`homely.stewardacs.xyz` given how many sessions have read them (not because
they're public — they aren't); (b) once decided, whether the two untracked
`opencode.json` files should be switched to read the token from an env var
instead of a literal value, then committed, or handled some other way. Not
touched further by any agent pending your call.

## Resolved this session (2026-09-06)

### GitHub remote for `house_designer`/`homely` — was already fine, just unpushed
`git remote -v` already had `origin` → `github.com/NaharEmet/homely.git`, and
`gh auth status` was already authenticated (account `NaharEmet`, scopes
include `repo`+`workflow`). The actual gap was that the local branch
(`fix/manager-driven-20260828`, which is also this repo's configured GitHub
default branch) was **138 commits ahead of origin, never pushed** — so CI had
never run against any of the Phase 1-2 work. Pushed this session; `ci.yml` is
now running for real on push. No action needed from you here.

## Group E — Infisical secrets/certificate manager setup (additive, not urgent-blocking)

Per the plan's new "Secrets and certificate management (Infisical, hard
exclusion for Hermees)" section (added 2026-09-06): adopting Infisical to
replace scattered plaintext `.env` files as the secrets/certificate source of
truth, with a hard two-tier access model enforced at the Infisical layer —
Hermees's own machine identity can read its operational secrets (Discord bot
token, model API keys, its three Postgres role connection strings) but has
ZERO access to certificates/PKI material or anything cert-adjacent
(Cloudflare API tokens, DNS/registrar credentials, payment processor keys),
structurally, not by convention — same "hard boundary, not just code
discipline" principle as the Phase 1 Observer Postgres role.

#### E1. Infisical account/project not provisioned yet
Same pattern as Neon (B1) and pgEdge (B2): this needs a real account only you
can create. **Needs:** an Infisical account + project, then a scoped Infisical
machine identity for Hermees limited to its operational-secrets path only
(Discord bot token, model API keys, `hermes_company`/`hermes_observer_writer`/
`hermes_analytics` connection strings) — with a separate project/environment/
path for certificates and anything cert-adjacent (Cloudflare API tokens,
DNS/registrar credentials, payment processor keys) that Hermees's identity has
no access policy granting it into at all, not even list/visibility.
This migration is additive, not urgent-blocking (existing `.env.example`
stays as the interim documentation of variable names/shapes); the two
already-known at-risk Steward tokens (Group D2, above) don't need to wait for
this to be rotated — that stays a separate, faster-moving task.

## Group F — Claude engineering-manager container credentials

#### F1. Headless Claude authentication not provisioned yet
The dedicated Claude engineering-manager container needs credentials for its
headless `claude` CLI. **Needs one of:** a real `ANTHROPIC_API_KEY`, or a
`CLAUDE_CODE_OAUTH_TOKEN` minted via `claude setup-token` to bill an existing
Claude subscription instead of a metered API key. Provide either value in the
real `.env`; no agent should invent or commit it.

#### F2. Gemini CLI not added — Google retired it June 2026, obsolete
Google retired Gemini CLI in June 2026; it is obsolete and was NOT added to
this container. Its successor, Antigravity CLI (`agy`, installed via
`curl -fsSL https://antigravity.google/cli/install.sh | bash` on Linux), is
not yet a decided tool in this architecture — Nahar is evaluating it locally
first. Do NOT add Antigravity to this container proactively; that is a future
ticket once he has actually tried it, not something to build ahead of now.

#### A3. Public rooms need a second gate (conversation vs. action-triggering) — not built yet, tracked for later (added 2026-09-07)
Per `/home/nahar/.claude/plans/ticklish-conjuring-horizon.md`'s Phase 2 "Public
rooms" paragraph: today `DISCORD_ALLOW_ALL_USERS`/`is_user_allowed()` is a
single binary gate — on, anyone who can reach the bot can both converse with
it AND trigger a real (non-dry-run) worker dispatch via `run_worker`; off,
neither. That's an accepted, deliberate simplification for testing on a
private server only Nahar is on (his call, live 2026-09-07: bridge run for
real with `DISCORD_ALLOW_ALL_USERS=true` and no `DISCORD_DM_USER` set — see
`company-ops/PLAN.md`'s dispatch-log note for that date). It stops being
acceptable the moment any Discord room/server the bot is in becomes public:
conversational replies (`ask_hermes`) should stay open to any public-room
user, but anything reaching `run_worker`/a real ticket dispatch must stay
gated to Nahar (or an explicit allow-list) independent of who's in the room.
**Needs:** a real second gate in `discord_bridge.py`'s `on_message` handler
(split the current single `is_allowed()`/`is_user_allowed()` check into a
conversation-access check and a separate, stricter dispatch-access check)
before any room is opened to the public. Not built yet — deliberately out of
scope for the 2026-09-07 live-testing dispatch; do not fold this into the
existing allow-list semantics as a quiet patch later, it needs its own
ticket on `company-ops/PLAN.md` when actually picked up.

## Group G — Discord bridge: real numeric Discord ID needed to re-open access (2026-09-07)

Live `discord_bridge.py` was just locked down to fully-closed (denies every
sender) at Nahar's request, pending his real numeric Discord user ID. His
given identifier "nahar5755" (with "i think") cannot be used: read
`discord_bridge.py` directly and confirmed the allow-list only ever
compares `str(message.author.id)` (numeric Discord snowflake, e.g.
`123456789012345678`) — there is no username/display-name matching code
path anywhere in `is_user_allowed`/`is_allowed`, so a username string would
never match even if it were correct.

**G1 — get your real numeric Discord user ID and give it here so
`company-ops/.env`'s `DISCORD_ALLOWED_USERS` can be set to it.** Steps:
Discord Settings -> Advanced -> enable Developer Mode, then right-click
your own name/avatar anywhere (server member list, a message, your own
profile) -> "Copy User ID". That numeric string is what goes in
`DISCORD_ALLOWED_USERS`.

Current interim state (fully closed, not "nahar5755"-keyed): `.env` has
`DISCORD_ALLOW_ALL_USERS=false` and
`DISCORD_ALLOWED_USERS=PENDING_REAL_SNOWFLAKE_ID_NOT_YET_PROVIDED` (a
placeholder value that can never match a real numeric ID, chosen instead
of leaving `DISCORD_ALLOWED_USERS` empty — see G2 below for why empty is
unsafe). Bridge process was restarted to pick this up (old PID 597973
killed, new PID 618593 confirmed reconnected: "Hermes Discord bridge
online as homely_ceo#9585"). Right now nobody, including you, can reach
the bot at all until G1 is done.

## Group G2 — latent fail-open bug in discord_bridge.py's allow-list check (found during G1, not yet fixed)

`is_user_allowed()`/`is_allowed()` in `discord_bridge.py` (lines ~86-101)
have this shape:
```
if ALLOW_ALL_USERS: return True
if ALLOWED_USERS and sender_id not in ALLOWED_USERS: return False
return True
```
If `ALLOWED_USERS` is empty (i.e. `DISCORD_ALLOWED_USERS` unset/blank) AND
`ALLOW_ALL_USERS=false`, the second `if` is falsy regardless of
`sender_id`, so both functions fall through to `return True` — i.e.
"disable the allow-all flag with no allow-list configured" silently
**allows everyone** instead of denying everyone. This is a footgun: the
intuitive-looking "safe default" of `ALLOW_ALL_USERS=false` +
`ALLOWED_USERS` unset does NOT lock the bridge down. Worked around for
now with the placeholder value above (forces the non-empty-set branch,
which correctly denies). Suggest a real fix later: flip the fallthrough so
an empty `ALLOWED_USERS` with `ALLOW_ALL_USERS=false` denies by default
(fail-closed), matching the intuitive semantics — a small, well-scoped
ticket for `discord_bridge.py` + `tests/test_discord_bridge.py` whenever
this area is next touched.

## Group G — UPDATE (2026-09-07, later same day): real numeric ID received, G1 resolved

Nahar provided his real numeric Discord user ID: `324293400851382273`
(valid 18-digit snowflake format). `company-ops/.env`'s
`DISCORD_ALLOWED_USERS` is now set to this real value (superseding the
`PENDING_REAL_SNOWFLAKE_ID_NOT_YET_PROVIDED` placeholder from the entry
above), with `DISCORD_ALLOW_ALL_USERS=false` unchanged. Bridge restarted
again (old PID 618593 killed, new PID 619447 confirmed reconnected:
"Hermes Discord bridge online as homely_ceo#9585"). Only Nahar's Discord
account (matching this numeric ID) can now reach the bot via DM or guild
mention; every other sender is denied by the existing non-empty-
`ALLOWED_USERS` branch. **G1 is resolved — no further action needed from
Nahar for this specific lockdown.** G2 (the fail-open bug when
`ALLOWED_USERS` is empty) remains open as a real but no-longer-urgent
code-quality fix for whenever `discord_bridge.py` is next touched.

## Group G2 — RESOLVED (2026-09-07)

Fixed in commit `a99fd01` (`fix(discord_bridge): deny by default when
allow-list is empty and allow-all is false`). `is_user_allowed()` and
`is_allowed()` in `discord_bridge.py` now fail closed: with
`DISCORD_ALLOWED_USERS` empty and `DISCORD_ALLOW_ALL_USERS` false/unset,
both functions return `False` for every sender instead of the old
fall-through `True`. The `ALLOWED_CHANNELS`-only restriction path in
`is_allowed()` is unaffected (a channel-only allow-list with no user
allow-list still behaves as before).

Verification performed independently (not just trusted from the
implementing worker's self-report):
- Read the actual diff in `company-ops/discord_bridge.py` and confirmed
  the logic against every case (allow-all true; non-empty user list, in/
  not-in; channel-only list, in/not-in; fully empty — the new deny case).
- `python3 -m unittest tests.test_discord_bridge -v` from `company-ops/`:
  26/26 passed (24 previously-existing behaviors unchanged +
  2 new tests for the empty-allow-list-denies case). The pre-existing
  `test_no_restrictions` tests in both `IsAllowedTests` and
  `IsUserAllowedTests` were correctly flipped from `assertTrue` to
  `assertFalse` (renamed to `test_no_restrictions_denies_by_default`) —
  they encoded the old buggy behavior and had to change, not just be left
  passing.
- Restarted the live bridge process end-to-end: `SIGTERM`'d the running
  PID (619447), confirmed exit, relaunched via
  `set -a; source .env; set +a; .venv/bin/python3 discord_bridge.py`
  (the same env-sourcing method the process was actually running under —
  the checked-in `hermes-discord.service` unit is not installed/enabled
  in systemd, so this manual method is the real deployment path today),
  confirmed the log shows `Hermes Discord bridge online as
  homely_ceo#9585` again, confirmed via `/proc/<pid>/environ` on the new
  PID that `DISCORD_ALLOW_ALL_USERS=false` and
  `DISCORD_ALLOWED_USERS=324293400851382273` (Nahar's real ID, unchanged)
  are in effect, and confirmed no duplicate/zombie `discord_bridge.py`
  processes remain after the restart.
- The workaround placeholder value mentioned in the original G2 entry
  above was already superseded by Nahar's real ID before this fix
  (see the G1 update below it); this fix makes that workaround
  unnecessary in general going forward — an empty `DISCORD_ALLOWED_USERS`
  with `DISCORD_ALLOW_ALL_USERS=false` now correctly denies everyone by
  default without needing a placeholder value to force it.

No further action needed on G2.

## Group H — Engineering container: repo access (GitHub App auth)

#### H1. ~~Mounted SSH deploy key lacks access to the new BuildMy-house repos~~ — RESOLVED / SUPERSEDED (2026-09-07)
SSH deploy key approach retired for the engineering container. Repo access
switched to GitHub App authentication (`buildmyhouse-engineering`, App ID `4857525`,
Installation ID `159686448`, private key `certs/buildmyhouse-engineering-app.pem`).
Tokens are minted automatically at container startup via `company-ops/scripts/github-app-token.js`
using RS256 JWTs and used for HTTPS clone/fetch (`x-access-token:<token>@github.com/...`).

**Action needed in GitHub App settings:**
To allow git clone and push on private repos (`company-os`), the GitHub App requires
**Repository permissions -> Contents: Read and write**:
1. Go to: `https://github.com/organizations/BuildMy-house/settings/apps/buildmyhouse-engineering/permissions`
2. Under **Repository permissions** -> **Contents**: select **"Read and write"**
3. Click **"Save changes"**
4. Go to: `https://github.com/organizations/BuildMy-house/settings/installations/159686448`
   and click **"Review and accept permissions"**

#### A4. Discord reply-auto-capture for Human Interface requests not built (found 2026-09-07)
`company_ops/human_interface.py`'s `ask_information`/`ask_judgment`/
`request_approval`/`request_action` each write an open `observer.human_requests`
row and send a real Discord DM, but there is no code path anywhere that
listens for Nahar's reply and calls `complete_human_interface_request()`
automatically. Confirmed by direct read of `discord_bridge.py`'s `on_message`:
every DM (including a genuine reply to a pending request) is routed straight
into `ask_hermes`/`run_worker` as a fresh worker-dispatch prompt, with no
lookup against open `observer.human_requests` rows first. Today, the only way
a request actually gets completed is a manual direct call to
`complete_human_interface_request()` (e.g. this session's live-test round-trip
in `PLAN.md`) -- not a real Discord reply. Not built this session, per explicit
coordinator direction to stop after verifying/fixing existing gaps rather than
add new capability. When picked up, needs its own scoped ticket: a minimal
design is "on a DM, check for the most recent open (uncompleted) human_request
and treat the message as its reply instead of dispatching a new worker prompt" --
deliberately not implemented here so it isn't done as a rushed side effect of
a different ticket.

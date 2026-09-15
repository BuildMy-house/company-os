# Hermes/company-ops operational gotchas

Real incidents, each one caught live in an operator session and costly
enough (lost time, or a bad push) to document so the next session doesn't
repeat them. Read this before touching Hermes/company-ops infrastructure —
especially before editing SOUL.md, `entrypoint.sh`, or anything that looks
like it lives in a "company-ops" directory.

## 1. `house_designer/company-ops/` is NOT the real source — verify before editing

This project was split from one monorepo into several per-purpose repos.
`company-ops` moved to its own repo, **`BuildMy-house/company-os`**. Several
local checkouts on the operator machine (`house_designer/company-ops/`, and
worktree-like directories such as `buildmyhouse-b1-2/3/4`) are **stale
pre-split leftovers or independently-diverged copies** — not connected to
the real deploy pipeline, and not kept in sync with each other.

**Symptom if you miss this:** you edit `company-ops/hermes/SOUL.md` (or any
file under a local `company-ops/`), it looks plausible, you commit it — and
it does nothing, because the actual running image was built from a
completely different, newer checkout of `company-os`.

**How to verify you're editing the real source, every time, before touching
anything:**
1. `gh repo list BuildMy-house` — confirm `company-os` is a real, separately
   pushed repo (check the push timestamp — if it's recent, it's alive).
2. Diff whatever local file you're about to edit against the **live,
   currently-running pod's copy** of the same file (`kubectl exec ... cat
   <path>`), not just against another local checkout. A mismatch means your
   local copy is not the deploy source — go find the real one
   (`gh repo clone BuildMy-house/company-os <scratch-dir>`) before editing
   anything.
3. Never assume a local directory is canonical just because it has git
   history for the path you care about — a stale copy can still have real,
   old commit history. Recency and remote match matter more than the
   presence of history.

If you find yourself with edits already made against a directory that turns
out to be stale: discard them (`git checkout --`) rather than trying to
port them by hand — redo the edit against a fresh clone of the real repo so
you're building on its actual current content, not reconstructing it from
memory.

## 2. SOUL.md's real load path is `$HERMES_HOME` (`/opt/data`), not `/root/.hermes`

hermes-agent's own `get_default_hermes_root()` reads `~/.hermes`, *or*
`HERMES_HOME` itself when that env var is set (this container sets
`HERMES_HOME=/opt/data`). An older `entrypoint.sh` wrote SOUL.md to
`/root/.hermes/SOUL.md` — a path hermes-agent never actually reads in this
deployment. Confirmed live: the pod ran on hermes-agent's own generic
auto-seeded default persona ("You are Hermes Agent, built by Nous
Research" — no mention of buildmy.house) for a real stretch of time while
the correct SOUL.md sat unread at the wrong path. This is already fixed in
`entrypoint.sh` (writes to `"${HERMES_HOME:-/opt/data}/SOUL.md"`) — but if
you ever see Hermes behaving like a generic assistant with no memory of
being CEO of buildmy.house, check this path mismatch first.

## 3. `github-app-token.js` needs the private key as a **file**, not an env var

`GITHUB_APP_PRIVATE_KEY` (raw PEM content) is set as an env var on both the
`engineering` and `company-ops` containers, but `scripts/github-app-token.js`
only ever reads the key from a **file** at one of a fixed list of paths
(default `/etc/github/buildmyhouse-engineering-app.pem`). The `engineering`
container gets this file written for it as a side effect of `ai-cli-mcp`'s
own setup step — `company-ops` (`hermes gateway run`) never ran that setup,
so any script there calling `github-app-token.js` silently fails to mint a
token unless something writes that file first. Confirmed live: a feature
depending on this (pulling `hermees-memory` early in `entrypoint.sh`) shipped
once, silently no-opped on every boot, and only started working after adding
an explicit write-the-pem-from-the-env-var step before calling the script.

**Before shipping anything that calls `github-app-token.js` from a new
context:** confirm the private-key file actually gets written somewhere
that script checks, or write it yourself
(`printf '%s\n' "$GITHUB_APP_PRIVATE_KEY" > /etc/github/buildmyhouse-engineering-app.pem`)
before calling it — don't assume the env var alone is enough. **Validate
against the live pod's real credentials before merging**, not just "no
error on build" — the first version of the `hermees-memory` pull passed
review and shipped with this exact bug because nobody ran the token-mint
step against a real pod before merging.

## 4. Hermes's own memory-sync auto-commits and auto-pushes whatever's sitting in `/opt/hermees-memory` — don't leave scratch/debug content there

`hermees-memory` (`BuildMy-house/hermees-memory`) is a real, public,
git-backed memory store that `hermes gateway run` itself clones early at
boot and periodically auto-commits + pushes (`chore(memory): sync
<timestamp>` commits, authored as `Hermes <hermes@buildmy.house>`) — this
sync is autonomous and not something an operator session controls or gets
asked about first.

**Concretely:** if you `kubectl cp` a file into a running pod's
`/opt/hermees-memory` working directory for any reason (testing, staging a
change before deciding it's correct), and you don't finish the job in the
same breath, Hermes's own background sync can pick it up and push it to the
real public repo on its own within minutes — before you've verified the
content is even right. This happened live: a stale/wrong SOUL.md, copied in
mid-investigation before its source was known to be wrong, got auto-pushed
to the public repo by Hermes itself. It was caught by checking
`hermees-memory`'s actual commit history on GitHub (`gh api
repos/BuildMy-house/hermees-memory/commits`) and fixed with a direct
correction commit — not by anything in the operator's own control.

**Rule of thumb:** treat any file placed into a running pod's
`/opt/hermees-memory` as effectively public and already-published the
moment it lands there — don't stage draft/uncertain content there "to
check it later." If you need to verify content before it's real, do that
verification in a separate scratch clone of `hermees-memory` (`gh repo
clone`), not inside the live pod's own working copy.

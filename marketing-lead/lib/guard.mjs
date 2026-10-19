// Push guard rules, shared by the git wrapper (bin/git) and the pre-push hook (hooks/pre-push).
// Policy: the marketing-lead may only create or update branches blog/<slug> or seo/<slug> on one of exactly three
// repos (ALLOWED_REPOS). Never main, never deletes, never force, never tags, never other remotes.
// Reports are NOT pushed to git at all: hand-back is the Hive result plus Steward memory.
//
// This is defense in depth against mistakes and prompt-injected behaviour. It is NOT a security
// boundary against an agent with a shell (it can call /usr/bin/git directly). The authoritative
// control is server side: a fine-grained token limited to these three repos plus branch
// protection on main (docs/MARKETING-LEAD.md).

// The only repositories the marketing-lead may touch, compiled in so a bad env value or ConfigMap edit
// cannot widen it. marketing-lead/config/marketing-lead.json must list exactly these (tested).
export const ALLOWED_REPOS = Object.freeze(["BuildMy-house/website", "BuildMy-house/hermees-diary", "BuildMy-house/observer-website"]);
export const repoUrl = (slug) => `https://github.com/${slug}`;

// blog/<slug>: blog posts. seo/<slug>: website/SEO changes and playbook experiment PRs.
export const BRANCH_RE = /^(blog|seo)\/[a-z0-9][a-z0-9-]{0,80}$/;
const ZERO_OID = /^0+$/;

// Exact option strings only. Abbreviations (--force-with-l, --del, --no-ver...) are therefore
// rejected rather than guessed at.
export const PUSH_OPTIONS_ALLOWED = new Set(["-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--progress"]);

// -c overrides the agent may pass. Everything else (core.hooksPath, alias.*, url.*, remote.*,
// push.*, credential.*, include*, core.sshCommand, ...) is refused.
export const CONFIG_OVERRIDES_ALLOWED = new Set(["user.name", "user.email", "commit.gpgsign", "core.quotepath", "init.defaultbranch", "advice.detachedhead"]);

// `git config` is read-only for the agent: identity and hooks come from /etc/gitconfig.
export const CONFIG_READ_FLAGS = new Set(["--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--local", "--global", "--system", "--null", "-z"]);

export function refuse(reason) {
  const e = new Error(reason);
  e.guard = true;
  return e;
}

export function checkBranchRef(ref) {
  const name = ref.replace(/^refs\/heads\//, "");
  if (!BRANCH_RE.test(name)) {
    throw refuse(`push refused: "${ref}" is not an allowed branch. The marketing-lead may only push blog/<slug> or seo/<slug> (slug: lowercase letters, digits, hyphens). It never pushes main; a human or Board-approved merge publishes.`);
  }
  return name;
}

// Validate `git <global opts> push ...`. `args` are the arguments after the "push" word.
// currentBranch() resolves HEAD for `git push origin HEAD`.
export function checkPushArgs(args, currentBranch) {
  const positional = [];
  for (const arg of args) {
    if (arg.startsWith("-") && arg !== "-") {
      if (!PUSH_OPTIONS_ALLOWED.has(arg)) throw refuse(`push refused: option "${arg}" is not allowed (allowed: ${[...PUSH_OPTIONS_ALLOWED].join(" ")}). No force, no --no-verify, no --all/--mirror/--tags/--delete.`);
    } else positional.push(arg);
  }
  const [remote, ...refspecs] = positional;
  if (!remote) throw refuse('push refused: name the remote and branch explicitly, e.g. "git push -u origin blog/<slug>" or "seo/<slug>".');
  if (!refspecs.length) throw refuse(`push refused: no explicit branch given for "${remote}". Use "git push -u ${remote} blog/<slug>" (or seo/<slug>).`);
  const dests = [];
  for (const spec of refspecs) {
    if (spec.startsWith("+")) throw refuse(`push refused: "${spec}" is a forced refspec.`);
    if (spec.includes("*")) throw refuse(`push refused: wildcard refspec "${spec}".`);
    if (spec.startsWith(":")) throw refuse(`push refused: "${spec}" deletes a remote ref.`);
    const colon = spec.indexOf(":");
    const src = colon === -1 ? spec : spec.slice(0, colon);
    let dst = colon === -1 ? spec : spec.slice(colon + 1);
    if (!src || !dst) throw refuse(`push refused: malformed refspec "${spec}".`);
    if (colon === -1 && src === "HEAD") dst = currentBranch();
    if (colon !== -1 && dst === "HEAD") throw refuse(`push refused: ambiguous destination in "${spec}".`);
    dests.push(checkBranchRef(dst));
  }
  return { remote, branches: dests };
}

// Global options before the subcommand. Returns { rest, hasC } with the subcommand first.
export function parseGlobalArgs(argv) {
  const kept = [];
  let i = 0;
  while (i < argv.length && argv[i].startsWith("-")) {
    const a = argv[i];
    if (a === "-C") {
      kept.push(a, argv[i + 1] ?? "");
      i += 2;
    } else if (a === "--no-pager" || a === "--no-optional-locks" || a === "-P") {
      kept.push(a);
      i += 1;
    } else if (a === "-c") {
      const kv = argv[i + 1] ?? "";
      const key = kv.split("=", 1)[0].toLowerCase();
      if (!CONFIG_OVERRIDES_ALLOWED.has(key)) throw refuse(`git refused: "-c ${key}" is not allowed (it could change hooks, remotes, credentials or aliases).`);
      kept.push(a, kv);
      i += 2;
    } else {
      throw refuse(`git refused: global option "${a}" is not allowed.`);
    }
  }
  return { globals: kept, rest: argv.slice(i) };
}

export function checkConfigArgs(args) {
  const positional = args.filter((a) => !a.startsWith("-"));
  for (const a of args) {
    if (a.startsWith("-") && !CONFIG_READ_FLAGS.has(a)) throw refuse(`git config refused: option "${a}" is not allowed; config is read-only for the marketing-lead.`);
  }
  // `git config key value` writes; one positional is a read.
  if (positional.length > 1 && !args.includes("--get-regexp")) throw refuse("git config refused: writing config is not allowed; it is read-only for the marketing-lead.");
}

// Aliases and unusual subcommands could re-enter push with different semantics.
export function checkSubcommand(sub) {
  if (!sub) return;
  if (sub === "daemon" || sub === "http-push" || sub === "send-pack" || sub === "receive-pack" || sub === "fast-import" || sub === "remote-http" || sub === "remote-https") {
    throw refuse(`git refused: "${sub}" is not allowed.`);
  }
}

export function normalizeRemoteUrl(url) {
  return String(url || "").trim().replace(/\/+$/, "").replace(/\.git$/, "");
}

// allowed: array of remote URLs from MARKETING_LEAD_ALLOWED_REMOTES (comma list). Empty means refuse.
// `hardAllowed` is the compiled-in repo list; the remote must be in BOTH lists, so the env var can
// narrow the set but never widen it beyond ALLOWED_REPOS.
export function checkRemoteUrl(url, allowed, hardAllowed = ALLOWED_REPOS.map(repoUrl)) {
  const norm = normalizeRemoteUrl(url);
  if (/^[a-z]+:\/\/[^/]*@/i.test(norm)) throw refuse("push refused: remote URL must not embed credentials.");
  const ok = allowed.map(normalizeRemoteUrl).filter(Boolean);
  if (!ok.length) throw refuse("push refused: MARKETING_LEAD_ALLOWED_REMOTES is not set, so no remote is allowed.");
  if (!ok.includes(norm)) throw refuse(`push refused: remote "${url}" is not the configured repo.`);
  if (!hardAllowed.map(normalizeRemoteUrl).includes(norm)) throw refuse(`push refused: remote "${url}" is not one of the allowed site repos (${ALLOWED_REPOS.join(", ")}).`);
}

// pre-push stdin: "<local ref> <local oid> <remote ref> <remote oid>" per line.
export function checkRefLines(text) {
  for (const line of text.split("\n").filter(Boolean)) {
    const [localRef, localOid, remoteRef] = line.split(" ");
    if (ZERO_OID.test(localOid || "")) throw refuse(`push refused: deleting "${remoteRef}" is not allowed.`);
    if (!remoteRef || !remoteRef.startsWith("refs/heads/")) throw refuse(`push refused: "${remoteRef}" is not a branch (tags and other refs are not allowed).`);
    checkBranchRef(remoteRef);
    void localRef;
  }
}

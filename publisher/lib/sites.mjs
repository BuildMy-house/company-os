// Working with the three allowlisted site repos (website, hermees-diary, observer-website):
// sync a checkout, branch off the base, verify with the repo's own CI commands, then commit,
// push blog/<slug> and open a PR. Never pushes the base branch; the guarded git wrapper and the
// pre-push hook enforce that independently (lib/guard.mjs). Merging is a human or Board action.
import { Refused } from "./approval.mjs";
import { ALLOWED_REPOS, BRANCH_RE } from "./guard.mjs";

export function findRepo(cfg, name) {
  const r = (cfg.repos || []).find((x) => x.name === name || x.repo === name);
  if (!r) throw new Refused(`unknown repo "${name}". Allowed: ${(cfg.repos || []).map((x) => x.name).join(", ")}`);
  if (!ALLOWED_REPOS.includes(r.repo)) throw new Refused(`repo "${r.repo}" is not on the compiled-in allowlist`);
  return r;
}

export const repoDir = (work, r) => `${work}/${r.name}`;

// run(args, {cwd}) is the guarded git. exists/clean come from the caller so this stays testable.
export function syncRepo({ r, work, run, exists, clean }) {
  const dir = repoDir(work, r);
  if (!exists(`${dir}/.git`)) {
    run(["clone", `https://github.com/${r.repo}.git`, dir], {});
  } else {
    if (!clean(dir)) throw new Refused(`${dir} has uncommitted changes; finish or discard them before syncing`);
    run(["fetch", "origin"], { cwd: dir });
  }
  run(["checkout", "-B", r.baseBranch, `origin/${r.baseBranch}`], { cwd: dir });
  return { dir, base: r.baseBranch };
}

export function startBranch({ r, work, run, branch }) {
  if (!BRANCH_RE.test(branch)) throw new Refused(`branch must match ${BRANCH_RE} (blog/<slug>, lowercase, digits, hyphens)`);
  const dir = repoDir(work, r);
  run(["checkout", "-B", branch, `origin/${r.baseBranch}`], { cwd: dir });
  return { dir, branch };
}

export async function openSitePr({ r, work, run, branch, message, title, body, token, fetchImpl = fetch, hasChanges }) {
  if (!BRANCH_RE.test(branch)) throw new Refused(`branch must match ${BRANCH_RE}`);
  if (!message) throw new Refused("--message (commit message) is required");
  const dir = repoDir(work, r);
  if (!hasChanges(dir)) throw new Refused("nothing to commit: edit files in the checkout first");
  run(["checkout", "-B", branch], { cwd: dir });
  run(["add", "-A"], { cwd: dir });
  run(["commit", "-m", message], { cwd: dir });
  run(["push", "-u", "origin", branch], { cwd: dir });
  const res = await fetchImpl(`https://api.github.com/repos/${r.repo}/pulls`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "buildmy-house-publisher" },
    body: JSON.stringify({ title: title || message, head: branch, base: r.baseBranch, body: body || "Change from the publisher agent. Not merged or deployed by the agent: review, then a human or Board-approved merge." }),
  });
  const j = await res.json();
  if (!res.ok) throw new Error(`GitHub PR failed: HTTP ${res.status} ${String(j.message || "").slice(0, 200)}`);
  return { pr: j.html_url, repo: r.repo, branch };
}

// A repo's CI checks, run in its checkout. Commands are fixed in config, never agent-supplied.
export function verifyCommands(r) {
  return (r.verify || []).map((c) => c.split(/\s+/));
}

// Merge policy for the reviewer-merger. Pure functions: every rule from docs/REVIEWER-MERGER.md
// lives here, in code, so a prompt cannot talk the agent out of it. Fails closed: any unknown or
// missing input is a refusal, never a pass.

// Repos the reviewer may merge into, compiled in so a ConfigMap edit cannot widen it.
export const ALLOWED_REPOS = Object.freeze(["BuildMy-house/website", "BuildMy-house/hermees-diary", "BuildMy-house/observer-website", "BuildMy-house/app", "BuildMy-house/company-os"]);
// Never a merge target, whatever the config says.
export const FORBIDDEN_BASES = Object.freeze(["prod", "production", "release"]);
// A PR touching any of these needs a human: they define the agent's own limits.
export const GUARDED_PATHS = Object.freeze([
  "reviewer/**", "docs/REVIEWER-MERGER.md", "k8s/base/reviewer*/**", "k8s/base/mcp-gateway/**",
  "k8s/base/*-secrets-sync/**", "k8s/base/*secrets-sync*/**", ".github/workflows/**", "updater/**", "k8s/base/updater*/**",
]);
export const MAX_FILES = 300;
const SECRET_PATTERNS = [
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/, /\bgh[pousr]_[A-Za-z0-9]{30,}\b/, /\bgithub_pat_[A-Za-z0-9_]{30,}\b/,
  /\bAKIA[0-9A-Z]{16}\b/, /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/, /\bsk-[A-Za-z0-9]{32,}\b/,
];

export function globToRe(glob) {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*\*/g, "\u0000").replace(/\*/g, "[^/]*").replace(/\u0000/g, ".*");
  return new RegExp(`^${esc}$`);
}
export const matchesAny = (path, globs) => globs.some((g) => globToRe(g).test(path));

export function findRepoCfg(cfg, slug) {
  if (!ALLOWED_REPOS.includes(slug)) return null;
  return (cfg.repos || []).find((r) => r.repo === slug) || null;
}

// Input: { repo, pr, files, checks, selfLogin, reviewNote } where pr/files/checks are GitHub API
// shapes (pulls.get, pulls.listFiles, checks.listForRef check_runs). Returns
// { ok, refusals[], needsSharedReview, sharedFiles[], headSha }.
export function evaluate({ cfg, repo, pr, files, checks, selfLogin, reviewNote }) {
  const refusals = [];
  const rc = findRepoCfg(cfg, repo);
  if (!rc) return { ok: false, refusals: [`repo "${repo}" is not one the reviewer may merge into`], needsSharedReview: false, sharedFiles: [], headSha: null };
  if (!selfLogin) refusals.push("REVIEWER_LOGIN is not set, so own-PR check cannot run");
  if (pr.state !== "open") refusals.push(`PR is ${pr.state}, not open`);
  if (pr.draft) refusals.push("PR is a draft");
  const base = pr.base?.ref;
  if (FORBIDDEN_BASES.includes(base) || !rc.baseBranches.includes(base)) refusals.push(`base branch "${base}" is not allowed (allowed: ${rc.baseBranches.join(", ")}; never prod)`);
  if (pr.head?.repo && pr.head.repo.full_name !== repo) refusals.push("PR comes from a fork; forks are not merged by the agent");
  if (selfLogin && pr.user?.login === selfLogin) refusals.push("PR was opened by the reviewer's own identity");
  if (pr.mergeable === false) refusals.push("PR is not mergeable (conflicts)");

  if (!Array.isArray(files) || files.length === 0) refusals.push("PR has no changed files");
  else if (files.length > MAX_FILES) refusals.push(`PR changes ${files.length} files (limit ${MAX_FILES}); too large to review safely`);
  const paths = (files || []).flatMap((f) => [f.filename, f.previous_filename].filter(Boolean));
  const guarded = paths.filter((p) => matchesAny(p, GUARDED_PATHS));
  if (guarded.length) refusals.push(`touches guarded paths that need a human: ${[...new Set(guarded)].slice(0, 8).join(", ")}`);
  for (const f of files || []) {
    if (f.status !== "removed" && f.changes > 0 && !f.patch && !/\.(png|jpe?g|gif|webp|ico|woff2?|pdf|svg|lock)$/i.test(f.filename) && !/package-lock\.json$/.test(f.filename)) {
      refusals.push(`diff for "${f.filename}" is not available (too large or binary), so it cannot be reviewed`);
    }
    if (f.patch && SECRET_PATTERNS.some((re) => re.test(f.patch))) refusals.push(`"${f.filename}" appears to contain a secret or private key`);
    if (/(^|\/)\.env(\.|$)/.test(f.filename)) refusals.push(`"${f.filename}" is an env file`);
  }

  const runs = checks?.check_runs;
  if (!Array.isArray(runs) || runs.length === 0) refusals.push("no check runs on the head commit");
  else {
    const bad = runs.filter((c) => c.status !== "completed" || !["success", "neutral", "skipped"].includes(c.conclusion));
    if (bad.length) refusals.push(`checks not green: ${bad.map((c) => `${c.name}=${c.conclusion || c.status}`).join(", ")}`);
    for (const name of rc.requiredChecks || []) {
      if (!runs.some((c) => c.name === name && c.conclusion === "success")) refusals.push(`required check "${name}" has not passed`);
    }
  }

  const sharedFiles = paths.filter((p) => matchesAny(p, rc.sharedPaths || []));
  const needsSharedReview = sharedFiles.length > 0;
  if (needsSharedReview && !(reviewNote && reviewNote.trim().length >= 20)) {
    refusals.push(`touches shared components (${[...new Set(sharedFiles)].slice(0, 5).join(", ")}); provide --review-note (at least 20 characters) recording your review`);
  }
  return { ok: refusals.length === 0, refusals, needsSharedReview, sharedFiles: [...new Set(sharedFiles)], headSha: pr.head?.sha || null };
}

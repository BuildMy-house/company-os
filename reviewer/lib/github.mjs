// Thin GitHub REST client for the reviewer. Every merge is pinned to the head sha that was
// reviewed, so a push after review makes GitHub reject the merge instead of merging unseen code.
import { evaluate, findRepoCfg } from "./policy.mjs";

export class Refused extends Error {}

export function client({ token, fetchImpl = fetch }) {
  const call = async (method, path, body) => {
    const res = await fetchImpl(`https://api.github.com${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "buildmy-house-reviewer" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let j = null;
    try { j = text ? JSON.parse(text) : null; } catch { /* leave null */ }
    if (!res.ok) throw new Error(`GitHub ${method} ${path}: HTTP ${res.status} ${String(j?.message || text).slice(0, 200)}`);
    return j;
  };
  const listFiles = async (repo, n) => {
    const all = [];
    for (let page = 1; page <= 4; page++) {
      const chunk = await call("GET", `/repos/${repo}/pulls/${n}/files?per_page=100&page=${page}`);
      all.push(...chunk);
      if (chunk.length < 100) break;
    }
    return all;
  };
  return {
    call,
    async load(repo, n) {
      const pr = await call("GET", `/repos/${repo}/pulls/${n}`);
      const [files, checks] = await Promise.all([listFiles(repo, n), call("GET", `/repos/${repo}/commits/${pr.head.sha}/check-runs?per_page=100`)]);
      return { pr, files, checks };
    },
    comment: (repo, n, body) => call("POST", `/repos/${repo}/issues/${n}/comments`, { body }),
    merge: (repo, n, sha, title) => call("PUT", `/repos/${repo}/pulls/${n}/merge`, { merge_method: "squash", sha, commit_title: title }),
  };
}

export async function review({ gh, cfg, repo, n, selfLogin, reviewNote }) {
  if (!findRepoCfg(cfg, repo)) throw new Refused(`repo "${repo}" is not one the reviewer may merge into`);
  const loaded = await gh.load(repo, n);
  return { ...evaluate({ cfg, repo, ...loaded, selfLogin, reviewNote }), pr: loaded.pr };
}

// Re-evaluates immediately before merging; the decision is never taken from an earlier call.
export async function merge({ gh, cfg, repo, n, selfLogin, reviewNote }) {
  const r = await review({ gh, cfg, repo, n, selfLogin, reviewNote });
  if (!r.ok) {
    await gh.comment(repo, n, `Reviewer-merger: not merging.\n\n${r.refusals.map((x) => `- ${x}`).join("\n")}`);
    throw new Refused(`not merged:\n${r.refusals.map((x) => `- ${x}`).join("\n")}`);
  }
  const note = r.needsSharedReview ? `\n\nShared-component review (${r.sharedFiles.join(", ")}): ${reviewNote.trim()}` : "";
  await gh.comment(repo, n, `Reviewer-merger: checks green, policy passed at ${r.headSha.slice(0, 7)}; merging (squash).${note}`);
  await gh.merge(repo, n, r.headSha, `${r.pr.title} (#${n})`);
  return { merged: true, repo, pr: n, sha: r.headSha };
}

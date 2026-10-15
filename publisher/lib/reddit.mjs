// Reddit publishing via the official OAuth2 API (script app). Gates, in order: approval_ref,
// subreddit allowlist, per-day cap. Self-promotion rules are in skills/reddit-rules.md and are a
// human judgement; code only enforces the allowlist and cap. fetchImpl is injectable for tests.
import { Refused, requireApproval } from "./approval.mjs";

export function checkRedditPost(post, cfg, ledger, now = new Date()) {
  requireApproval(post.approvalRef, "reddit publish");
  const sub = String(post.subreddit || "").replace(/^r\//, "");
  const allowed = (cfg.reddit.allowedSubreddits || []).map((s) => s.toLowerCase());
  if (!allowed.includes(sub.toLowerCase())) throw new Refused(`reddit publish refused: r/${sub} is not in the allowlist (${allowed.join(", ") || "empty: nothing is allowed until the owner configures it"}).`);
  if (!post.title || post.title.length > 300) throw new Refused("reddit publish refused: title missing or over 300 chars.");
  if (!post.text && !post.url) throw new Refused("reddit publish refused: give either text or url.");
  ledger.check("reddit", post.approvalRef, cfg.reddit.dailyPostCap, now);
  return sub;
}

export async function publishReddit(post, cfg, ledger, env, fetchImpl = fetch, now = new Date()) {
  const sub = checkRedditPost(post, cfg, ledger, now);
  const ua = { "User-Agent": cfg.reddit.userAgent.replace("REDDIT_USERNAME", env.REDDIT_USERNAME) };
  const basic = Buffer.from(`${env.REDDIT_CLIENT_ID}:${env.REDDIT_CLIENT_SECRET}`).toString("base64");
  const tok = await fetchImpl("https://www.reddit.com/api/v1/access_token", {
    method: "POST",
    headers: { ...ua, Authorization: `Basic ${basic}`, "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "password", username: env.REDDIT_USERNAME, password: env.REDDIT_PASSWORD }),
  });
  const tj = await tok.json();
  if (!tok.ok || !tj.access_token) throw new Error(`reddit token failed: HTTP ${tok.status}`);
  const body = new URLSearchParams({ api_type: "json", sr: sub, title: post.title, kind: post.url ? "link" : "self", resubmit: "false" });
  if (post.url) body.set("url", post.url); else body.set("text", post.text);
  const res = await fetchImpl("https://oauth.reddit.com/api/submit", { method: "POST", headers: { ...ua, Authorization: `bearer ${tj.access_token}` }, body });
  const rj = await res.json();
  const errors = rj?.json?.errors || [];
  if (!res.ok || errors.length) throw new Error(`reddit submit failed: HTTP ${res.status} ${JSON.stringify(errors).slice(0, 200)}`);
  ledger.append({ ts: now.toISOString(), channel: "reddit", approval_ref: post.approvalRef, subreddit: sub });
  return { url: rj.json?.data?.url || null };
}

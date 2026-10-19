// Hindsight client for the marketing-lead's own bank. Endpoints taken from the Hindsight client source
// (retain: POST /v1/default/banks/{bank}/memories, recall: .../memories/recall); not verified against
// a live server. One shared API key serves all banks; separation is by bank NAME ONLY.
export function hindsight(cfg, env, fetchImpl = fetch) {
  const base = `${cfg.hindsight.url.replace(/\/$/, "")}/v1/default/banks/${encodeURIComponent(cfg.hindsight.bank)}`;
  const headers = { "Content-Type": "application/json", ...(env.HINDSIGHT_API_KEY ? { Authorization: `Bearer ${env.HINDSIGHT_API_KEY}` } : {}) };
  const call = async (path, body) => {
    const r = await fetchImpl(base + path, { method: "POST", headers, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`hindsight ${path}: HTTP ${r.status}`);
    return r.json();
  };
  return {
    retain: (content, context) => { assertNoSecrets(content); return call("/memories", { items: [{ content, context }] }); },
    recall: (query) => call("/memories/recall", { query }),
  };
}

// One 2-3 day cycle as a single retained entry: hypothesis, change, measured result, lesson. The result of the
// PREVIOUS cycle's experiment is retained as `result` once it is measured; recall at cycle start finds it by topic.
export function formatCycle({ date, hypothesis, change, result, lesson }) {
  const miss = Object.entries({ date, hypothesis, change, result, lesson }).filter(([, v]) => !String(v ?? "").trim()).map(([k]) => k);
  if (miss.length) throw new Error(`cycle entry needs: ${miss.join(", ")} (use "pending" for a result not yet measurable)`);
  return `SEO cycle ${date}\nHypothesis: ${hypothesis}\nChange: ${change}\nResult: ${result}\nLesson: ${lesson}`;
}

// Memory must hold decisions and learnings, never credentials or raw analytics rows.
export function assertNoSecrets(text) {
  if (/(ghp_|github_pat_|xox[bp]-|AKIA[0-9A-Z]{12}|-----BEGIN [A-Z ]*PRIVATE KEY|postgres(ql)?:\/\/[^\s:]+:[^\s@]+@|Bearer\s+[A-Za-z0-9._-]{20,}|(password|secret|token|cookie)\s*[:=]\s*\S{6,})/i.test(text)) throw new Error("memory refused: content looks like a credential");
  if (text.split("\n").length > 60) throw new Error("memory refused: too long; keep entries short and topic-sized");
}

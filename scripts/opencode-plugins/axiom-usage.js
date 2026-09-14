// Ships compact token-usage and tool-call metrics to Axiom (dataset bmh-company)
// — the cross-tool usage dashboard alongside Hermes and Claude Code. Content-light
// by design: model/provider/tokens/cost and tool names only, no prompts/args/output.
// Registered globally (all opencode invocations in this container, not per-repo) via
// generate-agent-mcp-config.js, which writes this path into opencode.json's `plugin`
// array. Every hook body is failsafe — a broken network call must never interrupt
// an actual coding session.
export const AxiomUsage = async () => {
  const token = process.env.AXIOM_TOKEN;
  if (!token) return {};

  const dataset = process.env.AXIOM_DATASET || "bmh-company";
  const endpoint = `https://api.axiom.co/v1/datasets/${dataset}/ingest`;
  const queue = [];
  let flushing = false;

  async function flush() {
    if (flushing || queue.length === 0) return;
    flushing = true;
    const batch = queue.splice(0, queue.length);
    try {
      await fetch(endpoint, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify(batch),
      });
    } catch {
      // fail-open: usage tracking must never break a coding session
    } finally {
      flushing = false;
    }
  }
  setInterval(flush, 5000);

  function push(event) {
    try {
      queue.push({ _time: new Date().toISOString(), service: "opencode", ...event });
      if (queue.length >= 20) flush();
    } catch {
      // fail-open
    }
  }

  return {
    event: async ({ event }) => {
      try {
        if (event.type !== "message.updated") return;
        const info = event.properties?.info;
        if (!info || info.role !== "assistant" || !info.time?.completed) return;
        push({
          event: "llm_call",
          sessionID: info.sessionID,
          messageID: info.id,
          model: info.modelID,
          provider: info.providerID,
          tokens: info.tokens,
          cost: info.cost,
        });
      } catch {
        // fail-open
      }
    },
    "tool.execute.after": async (input) => {
      try {
        push({ event: "tool_call", sessionID: input.sessionID, callID: input.callID, tool: input.tool });
      } catch {
        // fail-open
      }
    },
  };
};

// Ships usage metrics AND full message/tool-call content to Axiom (dataset
// bmh-company) — the cross-tool activity dashboard alongside Hermes and Claude
// Code. Ships full prompt/response text and tool input/output by explicit
// decision (2026-09-18) — previously content-light (metadata only); the user
// asked to see full conversation content in Axiom across all three agent
// planes. This is real conversation content leaving the machine into a
// third-party SaaS — if that scope ever needs to be narrowed again, revert
// to shipping only the `llm_call`/`tool_call` metadata events below.
// Registered globally (all opencode invocations in this container, not per-repo) via
// generate-agent-mcp-config.js, which writes this path into opencode.json's `plugin`
// array. Every hook body is failsafe — a broken network call must never interrupt
// an actual coding session.
export const AxiomUsage = async () => {
  const token = process.env.AXIOM_TOKEN;
  if (!token) return {};

  const dataset = process.env.AXIOM_DATASET || "bmh-company";
  // bmh-company lives on Axiom's eu-central-1 edge deployment, not the default
  // api.axiom.co domain (HTTP 400 there) — same fix as hermes-plugins/axiom_usage
  // (commit be94cc6), applied here so opencode-side events actually reach it.
  const endpoint = `https://eu-central-1.aws.edge.axiom.co/v1/ingest/${dataset}`;
  const queue = [];
  let flushing = false;
  const roleByMessageID = new Map(); // populated from message.updated, read by message.part.updated

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

  // Every push MUST await flush() synchronously (not just rely on the 5s
  // interval below) — a one-shot `opencode run` CLI dispatch exits the
  // process immediately after the turn completes, killing any pending
  // setInterval before its next tick. Without this, the final (and often
  // only) llm_call/message events of a real dispatch were queued then
  // silently lost on exit, even though the plugin loaded and fired
  // correctly — this was the actual reason `role: "worker"` events never
  // reached Axiom for real dispatches despite exhaustive parity checks
  // finding nothing wrong with the plugin, config, or environment.
  // Verified live 2026-09-20: same class of flush-timing bug as the
  // Hermes-side fix (see hermes-plugins/axiom_usage's on_session_end hook).
  async function push(event) {
    try {
      queue.push({
        _time: new Date().toISOString(),
        service: process.env.AXIOM_SERVICE_NAME || "opencode",
        environment: process.env.DEPLOYMENT_ENVIRONMENT || "local",
        role: "worker",
        ...event,
      });
      await flush();
    } catch {
      // fail-open
    }
  }

  return {
    event: async ({ event }) => {
      try {
        if (event.type === "message.updated") {
          const info = event.properties?.info;
          if (!info) return;
          roleByMessageID.set(info.id, info.role);
          if (info.role !== "assistant" || !info.time?.completed) return;
          await push({
            event: "llm_call",
            sessionID: info.sessionID,
            messageID: info.id,
            model: info.modelID,
            provider: info.providerID,
            agent: info.agent,
            tokens: info.tokens,
            cost: info.cost,
          });
          return;
        }
        if (event.type === "message.part.updated") {
          const part = event.properties?.part;
          if (!part || part.type !== "text" || !part.time?.end) return;
          await push({
            event: "message",
            sessionID: part.sessionID,
            messageID: part.messageID,
            role: roleByMessageID.get(part.messageID) || "unknown",
            text: part.text,
          });
        }
      } catch {
        // fail-open
      }
    },
    "tool.execute.after": async (input, output) => {
      try {
        await push({
          event: "tool_call",
          sessionID: input.sessionID,
          callID: input.callID,
          tool: input.tool,
          args: input.args,
          title: output?.title,
          result: typeof output?.output === "string" ? output.output.slice(0, 8000) : output?.output,
        });
      } catch {
        // fail-open
      }
    },
  };
};

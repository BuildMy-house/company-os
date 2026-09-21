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
  const roleByMessageID = new Map(); // populated from message.updated, read by message.part.updated

  // `currentFlush` chains every flush attempt serially (instead of an
  // in-flight-guard boolean that DROPS an overlapping call) and `dispose()`
  // below awaits it — this is what actually makes delivery reliable for a
  // one-shot `opencode run` CLI dispatch, which exits the process as soon as
  // the session goes idle. Earlier attempt (awaiting flush() inline from each
  // push(), without a dispose hook or serial chaining) was NOT sufficient:
  // verified live 2026-09-20 that two pushes firing ~14ms apart produced two
  // overlapping flush() calls — the second saw the boolean `flushing` guard
  // still true from the first's in-flight fetch and returned immediately
  // without sending, and the process exited before the first fetch's promise
  // ever resolved (confirmed via instrumented trace: no FLUSH_RESULT/
  // FLUSH_ERROR ever logged for that request). The `dispose` hook (from
  // @opencode-ai/plugin's Hooks interface) is the one opencode actually
  // awaits before tearing the plugin instance down — the interval below
  // remains only as a best-effort belt-and-braces for long-lived sessions.
  let currentFlush = Promise.resolve();
  function flush() {
    if (queue.length === 0) return currentFlush;
    const batch = queue.splice(0, queue.length);
    currentFlush = currentFlush.then(() =>
      fetch(endpoint, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify(batch),
      }).catch(() => {
        // fail-open: usage tracking must never break a coding session
      })
    );
    return currentFlush;
  }
  setInterval(flush, 5000);

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
    dispose: async () => {
      try {
        await flush();
      } catch {
        // fail-open
      }
    },
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

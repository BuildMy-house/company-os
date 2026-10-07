#!/usr/bin/env node

// Small MCP member adapter for Hermes and other agent runtimes. Hive remains
// the source of truth; wait uses SSE and never polls.
import readline from "node:readline";
import { lookup } from "node:dns/promises";
import { createConnection } from "node:net";

const base = process.env.HIVE_URL || "http://hive-coordinator:4100";
const consumerId = process.env.HIVE_AGENT_ID || "hermees";

try {
  await request("/agents/register", {
    method: "POST",
    body: JSON.stringify({
      id: consumerId,
      endpoint: process.env.A2A_ENDPOINT || "http://hermes-gateway:8642",
      capabilities: { profile: process.env.AGENT_PROFILE || "communicator", modes: ["observe", "propose", "bid", "execute", "review"] },
    }),
  });
} catch (error) {
  console.error(`[hive-member] registration failed: ${error.message}`);
}

async function request(path, options = {}) {
  const response = await fetch(`${base}${path}`, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error || `Hive returned ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

// Client-side fail-fast for the hive_complete feedback contract
// (HIVE-GUIDANCE-FEEDBACK.md section 3): reject before any Hive HTTP.
const FEEDBACK_NUMBERS = ["estimated_cost", "actual_cost", "estimated_benefit", "actual_benefit"];

function validateFeedback(result) {
  const feedback = result?.feedback;
  if (!feedback || typeof feedback !== "object") throw new Error("hive_complete requires result.feedback { calibration, friction, suggested_guidance_change? } — see HIVE-GUIDANCE-FEEDBACK.md section 3");
  if (!feedback.calibration || typeof feedback.calibration !== "object" || FEEDBACK_NUMBERS.some((key) => typeof feedback.calibration[key] !== "number" || Number.isNaN(feedback.calibration[key]))) {
    throw new Error(`result.feedback.calibration requires numeric ${FEEDBACK_NUMBERS.join(", ")}`);
  }
  if (typeof feedback.friction !== "string") throw new Error('result.feedback.friction must be a string ("" if none)');
  if (feedback.suggested_guidance_change !== undefined && typeof feedback.suggested_guidance_change !== "string") throw new Error("result.feedback.suggested_guidance_change must be a string (omit or empty if none)");
}

// Fire-and-forget Steward forwarding (HIVE-GUIDANCE-FEEDBACK.md section 4):
// guidance change -> specs_propose review flow; calibration/friction only ->
// submit_task_feedback. Never blocks or fails hive_complete.
async function requestSteward(tool, args) {
  const response = await fetch(process.env.STEWARD_MCP_URL, {
    method: "POST",
    headers: { "content-type": "application/json", ...(process.env.STEWARD_TOKEN ? { authorization: `Bearer ${process.env.STEWARD_TOKEN}` } : {}) },
    body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method: "tools/call", params: { name: tool, arguments: args } }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error?.message || `Steward returned ${response.status}`);
  if (body.error) throw new Error(body.error.message || "Steward MCP error");
  return body.result;
}

function forwardFeedbackToSteward(taskId, state, feedback) {
  const hasChange = typeof feedback.suggested_guidance_change === "string" && feedback.suggested_guidance_change.trim() !== "";
  if (!process.env.STEWARD_MCP_URL || (!hasChange && feedback.friction === "")) return;
  void (async () => {
    try {
      if (hasChange) {
        await requestSteward("specs_propose", {
          app: "hive",
          path: `hive_guidance/${taskId}-${Date.now()}`,
          document_type: "hive_guidance",
          title: `hive_guidance change suggested by ${consumerId} for ${taskId}`,
          content: "```json\n" + JSON.stringify({ task_id: taskId, state, feedback }, null, 2) + "\n```",
        });
      } else {
        await requestSteward("submit_task_feedback", {
          agent_id: consumerId,
          task_id: taskId,
          learned_for_agents: `hive calibration=${JSON.stringify(feedback.calibration)}; friction=${feedback.friction}`,
        });
      }
    } catch (error) {
      console.error(`[hive-member] steward feedback forwarding failed for ${taskId}: ${error.message}`);
    }
  })();
}

function safeErrorDetails(error, stage) {
  const clean = (value) => String(value || "")
    .replace(/Bearer\s+\S+/gi, "Bearer [redacted]")
    .replace(/https?:\/\/[^\s"'<>]+/gi, "[url]")
    .slice(0, 400);
  return {
    name: clean(error?.name || "Error"),
    message: clean(error?.message || error),
    code: clean(error?.cause?.code || error?.code),
    cause: clean(error?.cause?.message),
    http_status: error?.status || error?.statusCode || null,
    stage,
  };
}

function reportSubscription(state, fields = {}) {
  send({
    jsonrpc: "2.0",
    method: "notifications/message",
    params: { level: "info", logger: "hive-member", data: { event: "hive_subscription", state, agent_id: consumerId, ...fields } },
  });
}

function checkTcp(host, port, timeoutMs) {
  return new Promise((resolve, reject) => {
    const socket = createConnection({ host, port });
    const timer = setTimeout(() => socket.destroy(new Error("TCP connect timed out")), timeoutMs);
    socket.once("connect", () => { clearTimeout(timer); socket.destroy(); resolve(); });
    socket.once("error", (error) => { clearTimeout(timer); reject(error); });
  });
}

async function diagnosticStep(name, timeoutMs, operation) {
  const started = Date.now();
  let timer;
  try {
    await Promise.race([
      operation(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(Object.assign(new Error(`${name} timed out`), { code: "ETIMEDOUT" })), timeoutMs); }),
    ]);
    return { step: name, ok: true, duration_ms: Date.now() - started };
  } catch (error) {
    return { step: name, ok: false, duration_ms: Date.now() - started, ...safeErrorDetails(error, name) };
  } finally {
    clearTimeout(timer);
  }
}

async function diagnoseHive(timeoutMs = 2_000) {
  const endpoint = new URL(base);
  const port = Number(endpoint.port || (endpoint.protocol === "https:" ? 443 : 80));
  const steps = [];
  steps.push(await diagnosticStep("dns", timeoutMs, () => lookup(endpoint.hostname)));
  steps.push(await diagnosticStep("tcp", timeoutMs, () => checkTcp(endpoint.hostname, port, timeoutMs)));
  steps.push(await diagnosticStep("http", timeoutMs, async () => {
    const response = await fetch(`${base}/.well-known/agent-card.json`, { signal: AbortSignal.timeout(timeoutMs) });
    await response.body?.cancel();
    if (!response.ok) throw Object.assign(new Error(`Hive returned ${response.status}`), { status: response.status });
  }));
  steps.push(await diagnosticStep("work_sse", timeoutMs, async () => {
    const response = await fetch(`${base}/work/subscribe?agent_id=${encodeURIComponent(consumerId)}`, {
      headers: { accept: "text/event-stream" },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const contentType = response.headers.get("content-type") || "";
    await response.body?.cancel();
    if (!response.ok) throw Object.assign(new Error(`Hive work stream returned ${response.status}`), { status: response.status });
    if (!contentType.includes("text/event-stream")) throw new Error(`unexpected work stream content-type: ${contentType}`);
  }));
  return { endpoint: endpoint.host, ok: steps.every((step) => step.ok), steps };
}

async function waitForEvent(taskId, timeoutSeconds = 900) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSeconds * 1000);
  try {
    const response = await fetch(`${base}/events/subscribe?task_id=${encodeURIComponent(taskId)}`, {
      signal: controller.signal,
      headers: { accept: "text/event-stream" },
    });
    if (!response.ok || !response.body) throw new Error(`Hive event stream returned ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) throw new Error("Hive event stream closed");
      buffer += decoder.decode(value, { stream: true });
      for (const frame of buffer.split("\n\n").slice(0, -1)) {
        const data = frame.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
        if (!data) continue;
        const event = JSON.parse(data);
        if (["engineering.completed", "engineering.failed"].includes(event.topic)) {
          await request(`/events/${encodeURIComponent(event.event_id)}/ack`, {
            method: "POST",
            body: JSON.stringify({ consumer_id: consumerId }),
          });
          return event;
        }
      }
      buffer = buffer.split("\n\n").at(-1) || "";
    }
  } finally {
    clearTimeout(timer);
  }
}

// Lifecycle guidance sent in the initialize response (HIVE-GUIDANCE-FEEDBACK.md
// section 1, plus the dynamic-guidance directive from the Phase 1 ticket).
const instructions = `Hive member tools for the company-os coordination service (Elixir/Postgres work, leases, durable events). Lifecycle: 1) hive_available_work lists open work; bid with hive_bid (confidence, approach, estimated_cost, expected_benefit). 2) Block, never poll: idle members wait inside hive_next_work; members awaiting a specific task wait inside hive_wait(task_id) — do NOT loop hive_status (one-off inspection only) or hive_available_work. 3) While holding a lease, renew with hive_heartbeat every few minutes and always before lease_seconds elapse (default 900) — an expired lease lets the task be re-allocated. 4) hive_complete is terminal: exactly one call per task with state completed|failed and a result.feedback object; after it the lease is gone and hive_wait returns. When any stream call errors, call hive_diagnostics first. hive_events replays durable history; hive_status reads current state; hive_work_bids audits who won and why. When Steward MCP tools are available, call generate_guidance_packet(scope_path: "hive") against Steward to fetch shared guidance before bidding (hive_bid).`;

const tools = [
  { name: "hive_submit", description: "Submit work to Hive and return its durable task id. After submitting, block on hive_wait(task_id) for the outcome — never poll hive_status.", inputSchema: { type: "object", required: ["message"], properties: { message: { type: "string" }, metadata: { type: "object" } }, additionalProperties: false } },
  { name: "hive_bid", description: "Submit or update this agent's bid for Hive work (confidence, approach, estimated_cost, expected_benefit). After bidding, wait for allocation via hive_next_work or hive_wait(work_id); once allocated, hive_heartbeat the task while working.", inputSchema: { type: "object", required: ["work_id", "confidence", "approach", "estimated_cost", "expected_benefit"], properties: { work_id: { type: "string" }, interested: { type: "boolean" }, confidence: { type: "number", minimum: 0, maximum: 1 }, approach: { type: "string" }, estimated_cost: { type: "number", minimum: 0 }, expected_benefit: { type: "number", minimum: 0 }, risk: { type: "string" }, evidence: { type: "string" } }, additionalProperties: false } },
  { name: "hive_work_bids", description: "Read the ranked bids (with each bidder's stored rationale) for Hive work, by id or slug. Call before hive_allocate to sanity-check the ranking, or after allocation to audit why a bidder won.", inputSchema: { type: "object", required: ["work_id"], properties: { work_id: { type: "string" }, limit: { type: "integer", minimum: 1, maximum: 100 } }, additionalProperties: false } },
  { name: "hive_available_work", description: "List currently available Hive work items (id, slug, payload). Pick items worth bidding on and call hive_bid; to learn of new arrivals, wait inside hive_next_work instead of re-calling this in a loop.", inputSchema: { type: "object", properties: { limit: { type: "integer", minimum: 1, maximum: 100 } }, additionalProperties: false } },
  { name: "hive_agents", description: "List registered Hive agents with their A2A endpoint and declared capabilities. Call before hive_submit when you need to route or verify a target agent.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "hive_allocate", description: "Allocate available Hive work to its highest-ranked interested bidder (creates a lease). The allocated agent should hive_heartbeat the returned task_id while working and hive_complete when done.", inputSchema: { type: "object", required: ["work_id"], properties: { work_id: { type: "string" }, lease_seconds: { type: "integer", minimum: 60, maximum: 3600 } }, additionalProperties: false } },
  { name: "hive_heartbeat", description: "Renew this agent's temporary Hive work lease (default 900s). Call every few minutes while still working, always before lease_seconds elapse — an expired lease lets the task be re-allocated. When finished, call hive_complete instead.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, lease_seconds: { type: "integer", minimum: 60, maximum: 3600 } }, additionalProperties: false } },
  { name: "hive_complete", description: "Record terminal completion or failure for work held by this agent; exactly one call per task, and the lease ends. result must include the required feedback object (calibration, friction, optional suggested_guidance_change). After this, hive_wait on the task returns immediately — use hive_status only to verify the recorded state.", inputSchema: { type: "object", required: ["task_id", "state", "result"], properties: { task_id: { type: "string" }, state: { type: "string" }, result: { type: "object", required: ["feedback"], additionalProperties: true, properties: { feedback: { type: "object", required: ["calibration", "friction"], additionalProperties: false, properties: { calibration: { type: "object", required: ["estimated_cost", "actual_cost", "estimated_benefit", "actual_benefit"], additionalProperties: false, properties: { estimated_cost: { type: "number" }, actual_cost: { type: "number" }, estimated_benefit: { type: "number" }, actual_benefit: { type: "number" } } }, friction: { type: "string", description: "What guidance was missing or wrong; \"\" if none." }, suggested_guidance_change: { type: "string", description: "Concrete change to guidance/specs; omit or empty if none." } } } } } }, additionalProperties: false } },
  { name: "hive_status", description: "Read the durable status of a Hive task — one-off inspection only, never poll in a loop. To block on completion use hive_wait; for the event history use hive_events.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" } }, additionalProperties: false } },
  { name: "hive_events", description: "Replay durable lifecycle events for a task (bids, allocation, heartbeats, completion). Use to debug a stuck task, then hive_status for current state or hive_wait to block on the outcome.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, limit: { type: "integer", minimum: 1, maximum: 100 } }, additionalProperties: false } },
  { name: "hive_wait", description: "Wait for a task's completion/failure event over SSE without polling (timeout up to 900s; re-call to keep waiting). This is the default way to await any task you submitted or hold — on timeout, re-call rather than switching to hive_status polling.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, timeout_seconds: { type: "integer", minimum: 5, maximum: 900 } }, additionalProperties: false } },
  { name: "hive_next_work", description: "Wait for the next available Hive work item over SSE without polling. Returns the allocated task — then hive_heartbeat it while working and hive_complete when done. Idle members should live inside this call, not loop on hive_available_work.", inputSchema: { type: "object", properties: { timeout_seconds: { type: "integer", minimum: 5, maximum: 900 } }, additionalProperties: false } },
  { name: "hive_diagnostics", description: "Check DNS, TCP, the Hive HTTP health route, and the worker SSE subscription from this container (opens and immediately closes an unclaimed stream; no work is consumed). Call this first when hive_wait or hive_next_work error or time out unexpectedly.", inputSchema: { type: "object", properties: { timeout_ms: { type: "integer", minimum: 250, maximum: 5000 } }, additionalProperties: false } },
];

function send(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }
function text(id, value) { return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] } }; }
function fail(id, message) { return { jsonrpc: "2.0", id, error: { code: -32000, message } }; }

async function call(name, args) {
  if (name === "hive_submit") return request("/", { method: "POST", body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method: "message/send", params: { message: { parts: [{ text: args.message }] }, ...(args.metadata ? { metadata: args.metadata } : {}) } }) });
  if (name === "hive_bid") { const { work_id, ...bid } = args; return request(`/work/${encodeURIComponent(work_id)}/bids`, { method: "POST", body: JSON.stringify({ agent_id: consumerId, ...bid }) }); }
  if (name === "hive_work_bids") return request(`/work/${encodeURIComponent(args.work_id)}/bids?limit=${args.limit || 100}`);
  if (name === "hive_available_work") return request(`/work?limit=${args.limit || 100}`);
  if (name === "hive_agents") return request("/agents");
  if (name === "hive_allocate") return request(`/work/${encodeURIComponent(args.work_id)}/allocate`, { method: "POST", body: JSON.stringify({ lease_seconds: args.lease_seconds || 900 }) });
  if (name === "hive_heartbeat") return request(`/work/${encodeURIComponent(args.task_id)}/heartbeat`, { method: "POST", body: JSON.stringify({ agent_id: consumerId, lease_seconds: args.lease_seconds || 900 }) });
  if (name === "hive_complete") {
    validateFeedback(args.result);
    const work = await request(`/work/${encodeURIComponent(args.task_id)}/complete`, { method: "POST", body: JSON.stringify({ agent_id: consumerId, state: args.state, result: args.result }) });
    forwardFeedbackToSteward(args.task_id, args.state, args.result.feedback);
    return work;
  }
  if (name === "hive_status") return request(`/tasks/${encodeURIComponent(args.task_id)}`);
  if (name === "hive_events") return request(`/events?task_id=${encodeURIComponent(args.task_id)}&limit=${args.limit || 100}`);
  if (name === "hive_wait") return waitForEvent(args.task_id, args.timeout_seconds || 900);
  if (name === "hive_next_work") return waitForWork(args.timeout_seconds || 900);
  if (name === "hive_diagnostics") return diagnoseHive(Math.min(5_000, Math.max(250, args.timeout_ms || 2_000)));
  throw new Error(`unknown tool: ${name}`);
}

async function waitForWork(timeoutSeconds = 900) {
  const startedAt = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSeconds * 1000);
  try {
    const response = await fetch(`${base}/work/subscribe?agent_id=${encodeURIComponent(consumerId)}`, {
      signal: controller.signal,
      headers: { accept: "text/event-stream" },
    });
    if (!response.ok || !response.body) throw new Error(`Hive work stream returned ${response.status}`);
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("text/event-stream")) throw new Error(`unexpected work stream content-type: ${contentType}`);
    reportSubscription("connected", { duration_ms: Date.now() - startedAt, stream: "work" });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) throw new Error("Hive work stream closed");
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const data = frame.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
        if (data) return JSON.parse(data);
      }
    }
  } finally {
    clearTimeout(timer);
  }
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let message;
  try { message = JSON.parse(line); } catch { continue; }
  if (message.method === "initialize") { send({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: message.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, instructions, serverInfo: { name: "hive-member", version: "0.1.0" } } }); continue; }
  if (message.method === "notifications/initialized" || message.method === "ping") { if (message.id !== undefined) send({ jsonrpc: "2.0", id: message.id, result: {} }); continue; }
  if (message.method === "tools/list") { send({ jsonrpc: "2.0", id: message.id, result: { tools } }); continue; }
  if (message.method === "tools/call") {
    try { send(text(message.id, await call(message.params.name, message.params.arguments || {}))); }
    catch (error) { send(fail(message.id, JSON.stringify(safeErrorDetails(error, message.params?.name)))); }
  }
}

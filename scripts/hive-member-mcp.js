#!/usr/bin/env node

// Small MCP member adapter for Hermes and other agent runtimes. Hive remains
// the source of truth; wait uses SSE and never polls.
import readline from "node:readline";
import { lookup } from "node:dns/promises";
import { createConnection } from "node:net";
import { existsSync, readdirSync } from "node:fs";

const base = process.env.HIVE_URL || "http://hive-coordinator:4100";
const consumerId = process.env.HIVE_AGENT_ID || "hermees";

// Register with Hive in the background with indefinite exponential-backoff
// retry (2s, 4s, 8s, 16s, capped at 60s). Never awaited at startup: the MCP
// stdio server below must come up immediately even while this is still
// retrying, so a transient hive-coordinator outage (e.g. ImagePullBackOff at
// pod boot) can't leave this worker permanently unregistered and silently
// unable to bid on Hive work (2026-10-08 incident).
const REGISTER_ESCALATE_AFTER = 5;

// Tool capabilities (docs/TICKET-STANDARD.md "Requiring tools"). Hive routes a ticket that
// lists requires_tools only to agents whose capabilities.tools declares all of them.
export const KNOWN_TOOLS = ["browser", "hindsight", "kubectl-deploy", "image-build", "hermes-data", "postgres-admin", "github-pr", "reddit-publish", "substack-publish", "marketing-db-read", "marketing-ingest"];

// Credential-gated tools (publisher pod): declared only when every listed env var is non-empty,
// so a pod without its Secret never advertises a tool it cannot use (same idea as the Chromium
// probe for "browser"). Tools not listed here are declared whenever AGENT_TOOLS names them.
// An entry "file:NAME" means env NAME must hold a path to an existing file. The publisher pod's
// sidecar (company_ops/marketing_sidecar.py) is the only holder of the Substack cookie and the
// ingest DSN; it writes these ready markers only after checking its own env and DB connection,
// so the main container never needs (or sees) those credentials to declare the tools.
export const TOOL_CREDENTIAL_ENV = {
  "github-pr": ["GITHUB_TOKEN"],
  "reddit-publish": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"],
  "substack-publish": ["file:SUBSTACK_READY_FILE"],
  "marketing-db-read": ["PUBLISHER_DATABASE_URL"],
  "marketing-ingest": ["file:MARKETING_INGEST_READY_FILE"],
};

function credentialMissing(tool, env) {
  return (TOOL_CREDENTIAL_ENV[tool] || []).some((name) => (name.startsWith("file:") ? !(env[name.slice(5)] && existsSync(env[name.slice(5)])) : !env[name]));
}

// A pod declares tools via AGENT_TOOLS (comma list, set in its manifest). Names must be known,
// and "browser" is only kept when a Chromium install is actually present, so a pod without
// Playwright never advertises a browser it cannot use.
export function resolveAgentTools(env = process.env, hasChromium = defaultHasChromium) {
  const declared = [...new Set(String(env.AGENT_TOOLS || "").split(",").map((t) => t.trim()).filter(Boolean))];
  const tools = [];
  for (const tool of declared) {
    if (!KNOWN_TOOLS.includes(tool)) console.error(`[hive-member] ignoring unknown AGENT_TOOLS entry "${tool}"`);
    else if (tool === "browser" && !hasChromium(env)) console.error('[hive-member] AGENT_TOOLS lists "browser" but no Chromium install was found; not declaring it');
    else if (credentialMissing(tool, env)) console.error(`[hive-member] AGENT_TOOLS lists "${tool}" but its credential env (${TOOL_CREDENTIAL_ENV[tool].join(", ")}) is incomplete; not declaring it`);
    else tools.push(tool);
  }
  return tools.sort();
}

function defaultHasChromium(env) {
  const dir = env.PLAYWRIGHT_BROWSERS_PATH || "/opt/ms-playwright";
  try {
    return existsSync(dir) && readdirSync(dir).some((entry) => entry.startsWith("chromium"));
  } catch {
    return false;
  }
}

async function registerWithHive() {
  let attempt = 0;
  for (;;) {
    attempt += 1;
    try {
      await request("/agents/register", {
        method: "POST",
        body: JSON.stringify({
          id: consumerId,
          endpoint: process.env.A2A_ENDPOINT || "http://hermes-gateway:8642",
          capabilities: {
            profile: process.env.AGENT_PROFILE || "communicator",
            modes: ["observe", "propose", "bid", "execute", "review"],
            // Tiering (hive/README.md "Worker tiers"): declared so the pool shows up in
            // hive_agents with its real flavor/tier/cost, not just the generic profile.
            flavor: process.env.AGENT_FLAVOR || "hermes",
            tier: ["cheap", "standard", "power"].includes(process.env.AGENT_TIER) ? process.env.AGENT_TIER : undefined,
            cost_factor: Number(process.env.AGENT_COST_FACTOR) > 0 ? Number(process.env.AGENT_COST_FACTOR) : undefined,
            tools: resolveAgentTools(),
          },
        }),
      });
      console.error(`[hive-member] registered with Hive after ${attempt} attempt${attempt === 1 ? "" : "s"}`);
      return;
    } catch (error) {
      const delayMs = Math.min(60_000, 2_000 * 2 ** (attempt - 1));
      const prefix = attempt >= REGISTER_ESCALATE_AFTER ? "[hive-member] WARNING:" : "[hive-member]";
      console.error(`${prefix} registration attempt ${attempt} failed: ${error.message}; retrying in ${Math.round(delayMs / 1000)}s`);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  }
}

// HIVE_REGISTER=false keeps this identity out of /agents/register entirely. The MCP gateway
// sets it for local (workstation) identities, which may read and hive_submit but must never
// appear as an allocatable worker (documents/plans/mcp-gateway-decisions item 3).
if (process.env.HIVE_REGISTER !== "false") void registerWithHive();

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

// Ticket standard (docs/TICKET-STANDARD.md): every hive_submit carries a type, story
// points, a goal, acceptance criteria and a scope, validated here before any Hive HTTP
// so a malformed ticket fails loudly instead of landing as an unbid, unscorable task.
const TICKET_TYPES = ["feature", "bug", "chore"];
const FIBONACCI_POINTS = [1, 2, 3, 5, 8, 13];
const BUG_BUMPS = { needs_diagnosis: 2, touches_production: 1 };
const MAX_POINTS = 13;

export function tierForPoints(points) {
  return points <= 3 ? "cheap" : points <= 5 ? "standard" : "power";
}

export function applyTicketStandard(args) {
  const metadata = { ...(args.metadata || {}) };
  const problems = [];
  if (!TICKET_TYPES.includes(metadata.type)) problems.push(`metadata.type must be one of ${TICKET_TYPES.join("|")}`);
  if (!FIBONACCI_POINTS.includes(metadata.points)) problems.push(`metadata.points must be one of ${FIBONACCI_POINTS.join(", ")} (split anything bigger than ${MAX_POINTS})`);
  const text = (value) => typeof value === "string" && value.trim() !== "";
  if (!text(metadata.goal)) problems.push("metadata.goal must state the outcome in one or two sentences");
  const criteria = Array.isArray(metadata.acceptance_criteria) ? metadata.acceptance_criteria.filter(text) : [];
  if (criteria.length === 0) problems.push("metadata.acceptance_criteria must list at least one verifiable check");
  if (!text(metadata.scope)) problems.push("metadata.scope must name the repo/paths in scope and what is out of scope");
  if (metadata.bug_modifiers !== undefined && (!Array.isArray(metadata.bug_modifiers) || metadata.bug_modifiers.some((m) => !(m in BUG_BUMPS)))) problems.push(`metadata.bug_modifiers may only contain ${Object.keys(BUG_BUMPS).join(", ")}`);
  if (metadata.requires_tools !== undefined) {
    const unknown = Array.isArray(metadata.requires_tools) ? metadata.requires_tools.filter((t) => !KNOWN_TOOLS.includes(t)) : null;
    if (unknown === null) problems.push("metadata.requires_tools must be an array of tool names");
    else if (unknown.length) problems.push(`metadata.requires_tools has unknown tool(s) ${unknown.map((t) => JSON.stringify(t)).join(", ")}; known tools: ${KNOWN_TOOLS.join(", ")}`);
  }
  if (problems.length) throw new Error(`hive_submit rejected, ticket does not meet docs/TICKET-STANDARD.md: ${problems.join("; ")}`);

  const bumps = metadata.type === "bug" ? [...new Set(metadata.bug_modifiers || [])].reduce((sum, m) => sum + BUG_BUMPS[m], 0) : 0;
  const points = metadata.points + bumps;
  if (points > MAX_POINTS) throw new Error(`hive_submit rejected: ${metadata.points} points + ${bumps} bug bump = ${points}, above ${MAX_POINTS}; split the ticket into smaller tickets`);

  const message = [
    args.message.trim(),
    `Type: ${metadata.type} | Points: ${points}${bumps ? ` (${metadata.points} + ${bumps} bug bump)` : ""}`,
    `Goal: ${metadata.goal.trim()}`,
    `Acceptance criteria:\n${criteria.map((c) => `- ${c.trim()}`).join("\n")}`,
    `Scope: ${metadata.scope.trim()}`,
  ].join("\n\n");
  // An explicit tier still wins, so a submitter can force power for work the points understate.
  const tier = ["cheap", "standard", "power"].includes(metadata.tier) ? metadata.tier : tierForPoints(points);
  const { bug_modifiers, requires_tools, ...rest } = metadata;
  const tools = requires_tools?.length ? { requires_tools: [...new Set(requires_tools)].sort() } : {};
  return { message, metadata: { ...rest, ...tools, points, base_points: metadata.points, tier } };
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
          document_type: "knowledge", // Steward rejects custom types; "knowledge" is the accepted enum for guidance docs
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
  { name: "hive_submit", description: "Submit work to Hive and return its durable task id. Tickets must follow docs/TICKET-STANDARD.md or are rejected: metadata.type (feature|bug|chore), metadata.points (1,2,3,5,8,13), metadata.goal, metadata.acceptance_criteria (array), metadata.scope; bugs may add metadata.bug_modifiers (needs_diagnosis +2, touches_production +1). Optional metadata.requires_tools (known: browser, hindsight, kubectl-deploy, image-build, hermes-data, postgres-admin, github-pr, reddit-publish, substack-publish, marketing-db-read, marketing-ingest) routes the ticket only to agents declaring all of them; if none exists it escalates to a human instead of dropping tier. Points set the tier unless metadata.tier is given. After submitting, block on hive_wait(task_id) for the outcome — never poll hive_status.", inputSchema: { type: "object", required: ["message", "metadata"], properties: { message: { type: "string" }, metadata: { type: "object", required: ["type", "points", "goal", "acceptance_criteria", "scope"], properties: { type: { type: "string", enum: TICKET_TYPES }, points: { type: "integer", enum: FIBONACCI_POINTS }, goal: { type: "string" }, acceptance_criteria: { type: "array", items: { type: "string" }, minItems: 1 }, scope: { type: "string" }, bug_modifiers: { type: "array", items: { type: "string", enum: Object.keys(BUG_BUMPS) } }, tier: { type: "string", enum: ["cheap", "standard", "power"] }, requires_tools: { type: "array", items: { type: "string", enum: KNOWN_TOOLS } }, approval_ref: { type: "string", description: "Board/Hermes approval reference; the publisher agent refuses to publish to Reddit or Substack without it (docs/PUBLISHER.md)." } } } }, additionalProperties: false } },
  { name: "hive_bid", description: "Submit or update this agent's bid for Hive work (confidence, approach, estimated_cost, expected_benefit). After bidding, wait for allocation via hive_next_work or hive_wait(work_id); once allocated, hive_heartbeat the task while working.", inputSchema: { type: "object", required: ["work_id", "confidence", "approach", "estimated_cost", "expected_benefit"], properties: { work_id: { type: "string" }, interested: { type: "boolean" }, confidence: { type: "number", minimum: 0, maximum: 1 }, approach: { type: "string" }, estimated_cost: { type: "number", minimum: 0 }, expected_benefit: { type: "number", minimum: 0 }, risk: { type: "string" }, evidence: { type: "string" } }, additionalProperties: false } },
  { name: "hive_work_bids", description: "Read the ranked bids for Hive work, by id, slug or task id (work_id, work_slug and task_id are interchangeable). Order is the allocation order: eligible bids cheapest first, each with eligible/ineligible_reasons/selected and the bidder's stored rationale. Call before hive_allocate to sanity-check the ranking, or after allocation to audit why a bidder won.", inputSchema: { type: "object", properties: { work_id: { type: "string" }, work_slug: { type: "string" }, task_id: { type: "string" }, limit: { type: "integer", minimum: 1, maximum: 100 } }, additionalProperties: false } },
  { name: "hive_cost_ledger", description: "Read-only cost ledger: per-agent allocations, failures (failed, lease_expired, escalated), runtime and total estimated cost, plus the most recent allocation rows with outcome. Use it to see whether cost-aware allocation is working; pair with hive_work_bids for one task.", inputSchema: { type: "object", properties: { days: { type: "integer", minimum: 1, maximum: 365 }, limit: { type: "integer", minimum: 1, maximum: 200 } }, additionalProperties: false } },
  { name: "hive_available_work", description: "List currently available Hive work items (id, slug, payload). Pick items worth bidding on and call hive_bid; to learn of new arrivals, wait inside hive_next_work instead of re-calling this in a loop.", inputSchema: { type: "object", properties: { limit: { type: "integer", minimum: 1, maximum: 100 } }, additionalProperties: false } },
  { name: "hive_agents", description: "List registered Hive agents with their A2A endpoint and declared capabilities. Call before hive_submit when you need to route or verify a target agent.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "hive_allocate", description: "Allocate available Hive work to the cheapest bidder that can deliver (creates a lease; see hive_work_bids for the order). While a connected pool worker has not bid yet the reply is {deferred: true, retry_after_seconds} and nothing is leased - wait and call again. The allocated agent should hive_heartbeat the returned task_id while working and hive_complete when done.", inputSchema: { type: "object", required: ["work_id"], properties: { work_id: { type: "string" }, lease_seconds: { type: "integer", minimum: 60, maximum: 3600 } }, additionalProperties: false } },
  { name: "hive_heartbeat", description: "Renew this agent's temporary Hive work lease (default 900s). Call every few minutes while still working, always before lease_seconds elapse — an expired lease lets the task be re-allocated. When finished, call hive_complete instead.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, lease_seconds: { type: "integer", minimum: 60, maximum: 3600 } }, additionalProperties: false } },
  { name: "hive_complete", description: "Record terminal completion or failure for work held by this agent; exactly one call per task, and the lease ends. result must include the required feedback object (calibration, friction, optional suggested_guidance_change). After this, hive_wait on the task returns immediately — use hive_status only to verify the recorded state.", inputSchema: { type: "object", required: ["task_id", "state", "result"], properties: { task_id: { type: "string" }, state: { type: "string" }, result: { type: "object", required: ["feedback"], additionalProperties: true, properties: { feedback: { type: "object", required: ["calibration", "friction"], additionalProperties: false, properties: { calibration: { type: "object", required: ["estimated_cost", "actual_cost", "estimated_benefit", "actual_benefit"], additionalProperties: false, properties: { estimated_cost: { type: "number" }, actual_cost: { type: "number" }, estimated_benefit: { type: "number" }, actual_benefit: { type: "number" } } }, friction: { type: "string", description: "What guidance was missing or wrong; \"\" if none." }, suggested_guidance_change: { type: "string", description: "Concrete change to guidance/specs; omit or empty if none." } } } } } }, additionalProperties: false } },
  { name: "hive_status", description: "Read the durable status of a Hive task — one-off inspection only, never poll in a loop. To block on completion use hive_wait; for the event history use hive_events.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" } }, additionalProperties: false } },
  { name: "hive_events", description: "Replay durable lifecycle events for a task (bids, allocation, heartbeats, completion). Use to debug a stuck task, then hive_status for current state or hive_wait to block on the outcome.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, limit: { type: "integer", minimum: 1, maximum: 100 } }, additionalProperties: false } },
  { name: "hive_wait", description: "Wait for a task's completion/failure event over SSE without polling (timeout up to 900s; re-call to keep waiting). This is the default way to await any task you submitted or hold — on timeout, re-call rather than switching to hive_status polling.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, timeout_seconds: { type: "integer", minimum: 5, maximum: 900 } }, additionalProperties: false } },
  { name: "hive_next_work", description: "Wait for the next available Hive work item over SSE without polling. Returns the allocated task — then hive_heartbeat it while working and hive_complete when done. Idle members should live inside this call, not loop on hive_available_work.", inputSchema: { type: "object", properties: { timeout_seconds: { type: "integer", minimum: 5, maximum: 900 } }, additionalProperties: false } },
  { name: "hive_diagnostics", description: "Check DNS, TCP, the Hive HTTP health route, and the worker SSE subscription from this container (opens and immediately closes an unclaimed stream; no work is consumed). Call this first when hive_wait or hive_next_work error or time out unexpectedly.", inputSchema: { type: "object", properties: { timeout_ms: { type: "integer", minimum: 250, maximum: 5000 } }, additionalProperties: false } },
  ...(process.env.AGENT_ROLE === "manager" ? [
    { name: "hive_prompt_workers", description: "Wake currently subscribed eligible workers for an available task. Returns worker identities prompted; does not bid or allocate on their behalf.", inputSchema: { type: "object", required: ["work_slug"], properties: { work_slug: { type: "string" } }, additionalProperties: false } },
  ] : []),
];

function send(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }
function text(id, value) { return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] } }; }
function fail(id, message) { return { jsonrpc: "2.0", id, error: { code: -32000, message } }; }

async function call(name, args) {
  if (name === "hive_submit") {
    if (typeof args.message !== "string" || args.message.trim() === "") throw new Error("hive_submit requires a non-empty message");
    const ticket = applyTicketStandard(args);
    return request("/", { method: "POST", body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method: "message/send", params: { message: { parts: [{ text: ticket.message }] }, metadata: ticket.metadata } }) });
  }
  if (name === "hive_bid") { const { work_id, ...bid } = args; return request(`/work/${encodeURIComponent(work_id)}/bids`, { method: "POST", body: JSON.stringify({ agent_id: consumerId, ...bid }) }); }
  if (name === "hive_work_bids") {
    const ref = args.work_id ?? args.work_slug ?? args.task_id;
    if (typeof ref !== "string" || ref === "") throw new Error("hive_work_bids requires work_id (or work_slug / task_id)");
    return request(`/work/${encodeURIComponent(ref)}/bids?limit=${args.limit || 100}`);
  }
  if (name === "hive_cost_ledger") return request(`/ledger?days=${args.days || 30}&limit=${args.limit || 20}`);
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
  if (name === "hive_prompt_workers") return request(`/work/${encodeURIComponent(args.work_slug)}/prompt`, { method: "POST", body: JSON.stringify({ requester_id: consumerId }) });
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
    if (!response.ok || !response.body) throw Object.assign(new Error(`Hive work stream returned ${response.status}`), { status: response.status });
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("text/event-stream")) throw new Error(`unexpected work stream content-type: ${contentType}`);
    reportSubscription("connected", { duration_ms: Date.now() - startedAt, stream: "work" });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        // Our own timeout aborts the stream — idle, not a failure.
        if (controller.signal.aborted) return null;
        throw new Error("Hive work stream closed");
      }
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const data = frame.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
        if (data) return JSON.parse(data);
      }
    }
  } catch (error) {
    if (controller.signal.aborted) return null;
    const details = safeErrorDetails(error, "hive_next_work");
    console.error(`[hive-member] hive_next_work failed: ${JSON.stringify(details)}`);
    reportSubscription("failed", details);
    throw error;
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

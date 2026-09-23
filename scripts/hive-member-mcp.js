#!/usr/bin/env node

// Small MCP member adapter for Hermes and other agent runtimes. Hive remains
// the source of truth; wait uses SSE and never polls.
import readline from "node:readline";

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
  if (!response.ok) throw new Error(body.error || `Hive returned ${response.status}`);
  return body;
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

const tools = [
  { name: "hive_submit", description: "Submit work to Hive and return its durable task id.", inputSchema: { type: "object", required: ["message"], properties: { message: { type: "string" }, metadata: { type: "object" } }, additionalProperties: false } },
  { name: "hive_status", description: "Read the durable status of a Hive task.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" } }, additionalProperties: false } },
  { name: "hive_events", description: "Replay durable lifecycle events for a task.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, limit: { type: "integer", minimum: 1, maximum: 100 } }, additionalProperties: false } },
  { name: "hive_wait", description: "Wait for a task completion/failure event over SSE without polling.", inputSchema: { type: "object", required: ["task_id"], properties: { task_id: { type: "string" }, timeout_seconds: { type: "integer", minimum: 5, maximum: 900 } }, additionalProperties: false } },
];

function send(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }
function text(id, value) { return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] } }; }
function fail(id, message) { return { jsonrpc: "2.0", id, error: { code: -32000, message } }; }

async function call(name, args) {
  if (name === "hive_submit") return request("/", { method: "POST", body: JSON.stringify({ jsonrpc: "2.0", id: Date.now(), method: "message/send", params: { message: { parts: [{ text: args.message }] }, ...(args.metadata ? { metadata: args.metadata } : {}) } }) });
  if (name === "hive_status") return request(`/tasks/${encodeURIComponent(args.task_id)}`);
  if (name === "hive_events") return request(`/events?task_id=${encodeURIComponent(args.task_id)}&limit=${args.limit || 100}`);
  if (name === "hive_wait") return waitForEvent(args.task_id, args.timeout_seconds || 900);
  throw new Error(`unknown tool: ${name}`);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let message;
  try { message = JSON.parse(line); } catch { continue; }
  if (message.method === "initialize") { send({ jsonrpc: "2.0", id: message.id, result: { protocolVersion: message.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "hive-member", version: "0.1.0" } } }); continue; }
  if (message.method === "notifications/initialized" || message.method === "ping") { if (message.id !== undefined) send({ jsonrpc: "2.0", id: message.id, result: {} }); continue; }
  if (message.method === "tools/list") { send({ jsonrpc: "2.0", id: message.id, result: { tools } }); continue; }
  if (message.method === "tools/call") {
    try { send(text(message.id, await call(message.params.name, message.params.arguments || {}))); }
    catch (error) { send(fail(message.id, error.message)); }
  }
}

#!/usr/bin/env node

// Small policy facade: Hermes gets a Claude-only engineering tool. Claude's
// own ai-cli MCP remains untouched, so Claude can still delegate internally.
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readdirSync, statfsSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import readline from "node:readline";

const upstream = spawn("npx", ["-y", "ai-cli-mcp@latest"], {
  stdio: ["pipe", "pipe", "inherit"],
  env: process.env,
});

const MANAGER = {
  flavor: process.env.AGENT_FLAVOR || "claude",
  role: process.env.AGENT_ROLE || "manager",
  agent: process.env.RUNNER_AGENT || (process.env.AGENT_FLAVOR === "opencode" ? "opencode" : "claude"),
  model: process.env.RUNNER_MODEL || (process.env.AGENT_FLAVOR === "opencode" ? "oc-opencode/mimo-v2.5-free" : "sonnet"),
  reasoning_effort: process.env.RUNNER_REASONING || "medium",
  auto_compact: "200k",
};
const capabilities = (process.env.AGENT_CAPABILITIES || "execute,review").split(",").map((value) => value.trim()).filter(Boolean);

const hiveMember = process.env.HIVE_URL
  ? spawn(process.execPath, [process.env.HIVE_MCP_SCRIPT || "/opt/company-ops/scripts/hive-member-mcp.js"], {
    stdio: ["pipe", "pipe", "inherit"],
    env: process.env,
  })
  : null;
const hivePending = new Map();
let hiveNextId = 1;
let hiveReady;

if (hiveMember) {
  hiveReady = new Promise((resolve, reject) => {
    hivePending.set(0, { resolve, reject });
    hiveMember.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 0, method: "initialize", params: { protocolVersion: "2025-03-26" } })}\n`);
  });
  const hiveInput = readline.createInterface({ input: hiveMember.stdout });
  hiveInput.on("line", (line) => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    const waiter = hivePending.get(message.id);
    if (!waiter) return;
    hivePending.delete(message.id);
    if (message.error) waiter.reject(new Error(message.error.message || "Hive MCP call failed"));
    else waiter.resolve(message);
  });
  hiveMember.on("error", (error) => {
    for (const waiter of hivePending.values()) waiter.reject(error);
    hivePending.clear();
  });
  hiveMember.on("exit", (code, signal) => {
    const error = new Error(`Hive MCP exited (${code ?? "signal " + signal})`);
    for (const waiter of hivePending.values()) waiter.reject(error);
    hivePending.clear();
  });
}

async function hiveCall(name, arguments_ = {}) {
  if (!hiveMember) throw new Error("Hive MCP is not configured");
  await hiveReady;
  const id = hiveNextId++;
  return new Promise((resolve, reject) => {
    hivePending.set(id, { resolve, reject });
    hiveMember.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: arguments_ } })}\n`);
  }).then(toolPayload);
}

const pending = new Map();
const a2aTasks = new Map();
let nextId = 1_000_000;

function emitTelemetry(event) {
  if (!process.env.AXIOM_TOKEN) return;
  fetch(`https://eu-central-1.aws.edge.axiom.co/v1/ingest/${process.env.AXIOM_DATASET || "bmh-company"}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.AXIOM_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify([{ _time: new Date().toISOString(), service: process.env.AXIOM_SERVICE_NAME || "engineering-manager", environment: process.env.DEPLOYMENT_ENVIRONMENT || "local", role: "manager", ...event }]),
  }).catch(() => {});
}

function toolPayload(message) {
  const text = message?.result?.content?.find((part) => part.type === "text")?.text;
  if (!text) return message?.result;
  try { return JSON.parse(text); } catch { return { output: text }; }
}

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function result(id, value) {
  return { jsonrpc: "2.0", id, result: value };
}

function error(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

function tool(name, description, inputSchema) {
  return { name, description, inputSchema };
}

function upstreamCall(name, arguments_, context = {}) {
  const id = nextId++;
  const started = Date.now();
  return new Promise((resolve, reject) => {
    pending.set(id, {
      resolve: (message) => {
        emitTelemetry({ event: "manager_upstream_call", call_id: id, tool: name, state: message.error ? "failed" : "completed", duration_ms: Date.now() - started, error: message.error?.message, ...context });
        resolve(message);
      },
      reject: (error) => {
        emitTelemetry({ event: "manager_upstream_call", call_id: id, tool: name, state: "failed", duration_ms: Date.now() - started, error: error.message, ...context });
        reject(error);
      },
    });
    emitTelemetry({ event: "manager_upstream_call", call_id: id, tool: name, state: "started" });
    upstream.stdin.write(`${JSON.stringify({
      jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: arguments_ },
    })}\n`);
  });
}

function health() {
  const binaries = MANAGER.flavor === "opencode"
    ? ["/usr/local/bin/opencode"]
    : ["/usr/local/bin/claude", "/usr/local/bin/opencode"];
  const workspace = "/workspace";
  return {
    status: binaries.every(existsSync) && existsSync(workspace) ? "healthy" : "degraded",
    manager: MANAGER,
    binaries: Object.fromEntries(binaries.map((path) => [path.split("/").pop(), existsSync(path)])),
    workspace: { path: workspace, exists: existsSync(workspace) },
    user: { uid: process.getuid?.() ?? null, gid: process.getgid?.() ?? null },
  };
}

function telemetry() {
  let filesystem = null;
  try {
    const stats = statfsSync("/workspace");
    filesystem = { total_bytes: stats.blocks * stats.bsize, free_bytes: stats.bfree * stats.bsize };
  } catch {}
  return {
    uptime_seconds: Math.round(os.uptime()),
    load_average: os.loadavg(),
    memory: { total_bytes: os.totalmem(), free_bytes: os.freemem() },
    process_count: readdirSync("/proc", { withFileTypes: true }).filter((entry) => /^\d+$/.test(entry.name)).length,
    filesystem,
  };
}

async function localCall(id, name) {
  if (name === "team_health") return send(result(id, { content: [{ type: "text", text: JSON.stringify(health(), null, 2) }] }));
  if (name === "container_telemetry") return send(result(id, { content: [{ type: "text", text: JSON.stringify(telemetry(), null, 2) }] }));
}

const input = readline.createInterface({ input: upstream.stdout });
input.on("line", (line) => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  const waiter = pending.get(message.id);
  if (!waiter) return;
  pending.delete(message.id);
  if (waiter.resolve) return waiter.resolve(message);
  if (waiter.original !== message.id) message.id = waiter.original;
  if (waiter.method === "tools/list" && message.result?.tools) {
    const run = message.result.tools.find((item) => item.name === "run");
    const visible = message.result.tools.filter((item) => !["run", "models"].includes(item.name));
    if (run) {
      const schema = structuredClone(run.inputSchema);
      delete schema.properties?.model;
      if (Array.isArray(schema.required)) schema.required = schema.required.filter((name) => name !== "model");
      visible.unshift(tool(
        "engineering",
        `Dispatch work to the ${MANAGER.flavor} ${MANAGER.role}. It may delegate workers internally when configured.`,
        schema,
      ));
    }
    visible.push(tool("team_health", `Check ${MANAGER.flavor} ${MANAGER.role}, workspace, identity, and worker-binary health.`, { type: "object", properties: {}, additionalProperties: false }));
    visible.push(tool("container_telemetry", "Get lightweight engineering-container uptime, load, memory, process, and disk telemetry.", { type: "object", properties: {}, additionalProperties: false }));
    message.result.tools = visible;
  }
  send(message);
});

upstream.on("error", (error) => {
  emitTelemetry({ event: "manager_upstream_process", state: "failed", error: error.message });
  for (const waiter of pending.values()) waiter.reject(error);
  pending.clear();
});

upstream.on("exit", (code, signal) => {
  const error = new Error(`ai-cli-mcp exited (${code ?? "signal " + signal})`);
  emitTelemetry({ event: "manager_upstream_process", state: "failed", error: error.message });
  for (const waiter of pending.values()) waiter.reject(error);
  pending.clear();
});

function a2aResponse(task) {
  return {
    id: task.id,
    status: { state: task.state },
    ...(task.result ? { artifacts: [{ parts: [{ text: JSON.stringify(task.result) }] }] } : {}),
    ...(task.error ? { status: { state: "failed", message: { parts: [{ text: task.error }] } } } : {}),
  };
}

function trackProcess(task, pid, onDone = () => {}) {
  const agentId = process.env.HIVE_AGENT_ID || "engineering-agent";
  const heartbeat = process.env.HIVE_URL ? setInterval(() => {
    hiveCall("hive_heartbeat", { task_id: task.id, lease_seconds: 900 })
      .catch((error) => emitTelemetry({ event: "hive_lease", task_id: task.id, state: "failed", error: error.message }));
  }, 60_000) : null;
  const finish = (value) => {
    if (heartbeat) clearInterval(heartbeat);
    onDone(value);
  };
  const poll = () => upstreamCall("get_result", { pid, verbose: true }, { task_id: task.id }).then((reply) => {
    const payload = toolPayload(reply);
    if (["completed", "failed", "killed"].includes(payload?.status)) {
      task.state = payload.status === "completed" ? "completed" : "failed";
      task.result = payload;
      for (const call of payload.agentOutput?.tools || []) {
        const output = typeof call.output === "string" ? call.output : "";
        const error = call.error || (/\bExit (?:code|status)\s+[1-9]\d*\b/i.test(output) ? output.match(/\bExit (?:code|status)\s+[1-9]\d*\b/i)?.[0] : null);
        emitTelemetry({ event: "tool_call", task_id: task.id, tool_name: call.tool, state: error ? "failed" : "completed", args: call.input, result: call.output, error });
      }
      if (task.state === "failed") task.error = payload.error || `agent process ${payload.status}`;
      emitTelemetry({ event: "a2a_task", task_id: task.id, state: task.state, duration_ms: Date.now() - task.startedAt, pid, error: task.error });
      finish(task);
      return;
    }
    setTimeout(poll, 2000);
  }).catch((error) => {
    task.state = "failed";
    task.error = error.message;
    emitTelemetry({ event: "a2a_task", task_id: task.id, state: "failed", duration_ms: Date.now() - task.startedAt, pid, error: task.error });
    finish(task);
  });

  setTimeout(poll, 1000);
}

function sendHttp(response, status, body) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(body));
}

async function readJson(request) {
  let body = "";
  for await (const chunk of request) body += chunk;
  return JSON.parse(body || "{}");
}

async function handleA2A(request, response) {
  const url = new URL(request.url, "http://127.0.0.1");
  if (request.method === "GET" && url.pathname === "/.well-known/agent-card.json") {
    return sendHttp(response, 200, {
      name: "buildmy.house engineering agent",
      description: `${MANAGER.flavor} engineering manager with ai-cli-mcp worker dispatch`,
      url: `http://${process.env.A2A_HOST || "engineering-agent"}:${process.env.A2A_PORT || 8001}`,
      version: "0.1.0",
      capabilities: { streaming: false, pushNotifications: false },
      skills: [{ id: "engineering", name: "Engineering work" }],
    });
  }
  if (request.method === "GET" && url.pathname.startsWith("/tasks/")) {
    const task = a2aTasks.get(url.pathname.slice("/tasks/".length));
    return task ? sendHttp(response, 200, a2aResponse(task)) : sendHttp(response, 404, { error: "task not found" });
  }
  if (request.method !== "POST" || url.pathname !== "/") return sendHttp(response, 404, { error: "not found" });

  let message;
  try { message = await readJson(request); } catch { return sendHttp(response, 400, { error: "invalid JSON" }); }
  if (message.jsonrpc !== "2.0" || message.method !== "message/send") {
    return sendHttp(response, 400, { jsonrpc: "2.0", id: message.id ?? null, error: { code: -32600, message: "expected message/send" } });
  }

  const taskId = `task_${randomUUID()}`;
  const parts = message.params?.message?.parts || [];
  const prompt = parts.filter((part) => typeof part.text === "string").map((part) => part.text).join("\n");
  const arguments_ = { workFolder: "/workspace", ...(message.params?.metadata || {}), prompt };
  const task = { id: taskId, state: "working", startedAt: Date.now() };
  a2aTasks.set(taskId, task);
  emitTelemetry({ event: "a2a_task", task_id: taskId, state: "working" });
  upstreamCall("run", { ...arguments_, agent: MANAGER.agent, model: MANAGER.model }, { task_id: taskId }).then((result) => {
    if (result.error) {
      task.state = "failed";
      task.error = result.error.message || "engineering request failed";
      emitTelemetry({ event: "a2a_task", task_id: taskId, state: "failed", duration_ms: Date.now() - task.startedAt, error: task.error });
      return;
    }
    const started = toolPayload(result);
    if (started?.status === "started" && Number.isInteger(started.pid)) {
      task.pid = started.pid;
      trackProcess(task, started.pid);
      return;
    }
    task.state = "failed";
    task.error = "engineering runner did not return a process id";
    emitTelemetry({ event: "a2a_task", task_id: taskId, state: "failed", duration_ms: Date.now() - task.startedAt, error: task.error });
  }).catch((error) => {
    task.state = "failed";
    task.error = error.message;
    emitTelemetry({ event: "a2a_task", task_id: taskId, state: "failed", duration_ms: Date.now() - task.startedAt, error: task.error });
  });
  return sendHttp(response, 200, { jsonrpc: "2.0", id: message.id, result: a2aResponse(task) });
}

http.createServer((request, response) => {
  handleA2A(request, response).catch((error) => sendHttp(response, 500, { error: error.message }));
}).listen(Number(process.env.A2A_PORT || 8001), "0.0.0.0");

let hiveBusy = false;
async function runHiveWork(candidate) {
  if (hiveBusy) return;
  try {
    const agentId = process.env.HIVE_AGENT_ID || "engineering-agent";
    await hiveCall("hive_bid", {
      work_id: candidate.id,
      interested: true,
      confidence: Number(process.env.HIVE_BID_CONFIDENCE || 0.7),
      approach: `${MANAGER.flavor} ${MANAGER.role} execution`,
      estimated_cost: Number(process.env.HIVE_BID_COST || 1),
      expected_benefit: Number(process.env.HIVE_BID_BENEFIT || 1),
      risk: process.env.HIVE_BID_RISK || "medium"
    });
    emitTelemetry({ event: "hive_work", task_id: candidate.id, state: "bid_submitted", agent_id: agentId });
    const allocated = await hiveCall("hive_allocate", { work_id: candidate.id, lease_seconds: 900 });
    if (allocated.claimed_by !== agentId) {
      emitTelemetry({ event: "hive_work", task_id: candidate.id, state: "allocation_lost", agent_id: agentId, claimed_by: allocated.claimed_by });
      return;
    }
    emitTelemetry({ event: "hive_work", task_id: candidate.id, state: "allocated", agent_id: agentId });
    hiveBusy = true;
    const task = { id: candidate.id, state: "working", startedAt: Date.now() };
    const prompt = candidate.payload?.parts?.filter((part) => typeof part.text === "string").map((part) => part.text).join("\n") || "Complete the assigned work.";
    upstreamCall("run", { workFolder: "/workspace", prompt, agent: MANAGER.agent, model: MANAGER.model }, { task_id: task.id }).then((reply) => {
      const started = toolPayload(reply);
      if (reply.error || started?.status !== "started" || !Number.isInteger(started.pid)) throw new Error(reply.error?.message || "engineering runner did not return a process id");
      task.pid = started.pid;
      trackProcess(task, started.pid, (finished) => hiveCall("hive_complete", { task_id: candidate.id, state: finished.state, result: finished.result || { error: finished.error } }).then(() => emitTelemetry({ event: "hive_work", task_id: candidate.id, state: finished.state, agent_id: process.env.HIVE_AGENT_ID || "engineering-agent" })).catch((error) => emitTelemetry({ event: "hive_completion", task_id: candidate.id, state: "failed", error: error.message })).finally(() => { hiveBusy = false; }));
    }).catch((error) => {
      hiveCall("hive_complete", { task_id: candidate.id, state: "failed", result: { error: error.message } }).catch(() => {}).finally(() => { hiveBusy = false; });
    });
  } catch (error) {
    emitTelemetry({ event: "hive_subscription", state: "failed", error: error.message });
  }
}

async function subscribeHive() {
  if (!hiveMember) return;
  const agentId = process.env.HIVE_AGENT_ID || "engineering-agent";
  try {
    emitTelemetry({ event: "hive_subscription", state: "connected", agent_id: agentId });
    while (true) {
      const candidate = await hiveCall("hive_next_work", { timeout_seconds: 900 });
      await runHiveWork(candidate);
    }
  } catch (error) {
    emitTelemetry({ event: "hive_subscription", state: "failed", error: error.message });
  }
  setTimeout(subscribeHive, 1000);
}
if (hiveMember) subscribeHive();

const requests = readline.createInterface({ input: process.stdin });
requests.on("line", async (line) => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  if (message.method === "tools/call") {
    const name = message.params?.name;
    if (name === "team_health" || name === "container_telemetry") return localCall(message.id, name);
    if (name === "engineering") {
      const args = { ...(message.params.arguments ?? {}), agent: MANAGER.agent, model: MANAGER.model };
      upstreamCall("run", args).then((reply) => {
        reply.id = message.id;
        send(reply);
      }).catch((err) => send(error(message.id, -32000, err.message)));
      return;
    }
    if (name === "run" || name === "models") return send(error(message.id, -32601, `${name} is not exposed to Hermes`));
  }
  const id = message.id;
  if (id !== undefined) pending.set(id, { original: id, method: message.method });
  upstream.stdin.write(`${line}\n`);
});

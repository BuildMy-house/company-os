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

const MANAGER = { agent: "claude", model: "sonnet", reasoning_effort: "medium", auto_compact: "200k" };

const pending = new Map();
const a2aTasks = new Map();
let nextId = 1_000_000;

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

function upstreamCall(name, arguments_) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    upstream.stdin.write(`${JSON.stringify({
      jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: arguments_ },
    })}\n`);
  });
}

function health() {
  const binaries = ["/usr/local/bin/claude", "/usr/local/bin/opencode"];
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
        "Dispatch work to the Claude Sonnet engineering manager only. The manager may delegate workers internally.",
        schema,
      ));
    }
    visible.push(tool("team_health", "Check Claude manager, workspace, identity, and worker-binary health.", { type: "object", properties: {}, additionalProperties: false }));
    visible.push(tool("container_telemetry", "Get lightweight engineering-container uptime, load, memory, process, and disk telemetry.", { type: "object", properties: {}, additionalProperties: false }));
    message.result.tools = visible;
  }
  send(message);
});

function a2aResponse(task) {
  return {
    id: task.id,
    status: { state: task.state },
    ...(task.result ? { artifacts: [{ parts: [{ text: JSON.stringify(task.result) }] }] } : {}),
    ...(task.error ? { status: { state: "failed", message: { parts: [{ text: task.error }] } } } : {}),
  };
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
      description: "Claude engineering manager with OpenCode worker dispatch",
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
  const task = { id: taskId, state: "working" };
  a2aTasks.set(taskId, task);
  upstreamCall("run", { ...arguments_, agent: MANAGER.agent, model: MANAGER.model }).then((result) => {
    task.state = result.error ? "failed" : "completed";
    if (result.error) task.error = result.error.message || "engineering request failed";
    else task.result = result.result;
  }).catch((error) => {
    task.state = "failed";
    task.error = error.message;
  });
  return sendHttp(response, 200, { jsonrpc: "2.0", id: message.id, result: a2aResponse(task) });
}

http.createServer((request, response) => {
  handleA2A(request, response).catch((error) => sendHttp(response, 500, { error: error.message }));
}).listen(Number(process.env.A2A_PORT || 8001), "0.0.0.0");

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

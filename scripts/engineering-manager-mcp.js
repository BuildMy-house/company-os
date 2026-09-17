#!/usr/bin/env node

// Small policy facade: Hermes gets a Claude-only engineering tool. Claude's
// own ai-cli MCP remains untouched, so Claude can still delegate internally.
import { spawn } from "node:child_process";
import { existsSync, readdirSync, statfsSync } from "node:fs";
import os from "node:os";
import readline from "node:readline";

const upstream = spawn("npx", ["-y", "ai-cli-mcp@latest"], {
  stdio: ["pipe", "pipe", "inherit"],
  env: process.env,
});

const pending = new Map();
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

function health() {
  const binaries = ["/usr/local/bin/claude", "/usr/local/bin/opencode"];
  const workspace = "/workspace";
  return {
    status: binaries.every(existsSync) && existsSync(workspace) ? "healthy" : "degraded",
    manager: { agent: "claude", model: "sonnet", reasoning_effort: "medium", auto_compact: "200k" },
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

const requests = readline.createInterface({ input: process.stdin });
requests.on("line", async (line) => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  if (message.method === "tools/call") {
    const name = message.params?.name;
    if (name === "team_health" || name === "container_telemetry") return localCall(message.id, name);
    if (name === "engineering") {
      const id = nextId++;
      const args = { ...(message.params.arguments ?? {}), model: "manager" };
      pending.set(id, { original: message.id, method: "tools/call" });
      upstream.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call", params: { name: "run", arguments: args } })}\n`);
      return;
    }
    if (name === "run" || name === "models") return send(error(message.id, -32601, `${name} is not exposed to Hermes`));
  }
  const id = message.id;
  if (id !== undefined) pending.set(id, { original: id, method: message.method });
  upstream.stdin.write(`${line}\n`);
});

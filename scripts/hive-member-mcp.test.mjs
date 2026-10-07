import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import readline from "node:readline";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const memberScript = fileURLToPath(new URL("./hive-member-mcp.js", import.meta.url));

test("hive_diagnostics checks DNS, TCP, HTTP, and SSE without consuming work", async (t) => {
  const server = createServer((request, response) => {
    if (request.method === "POST" && request.url === "/agents/register") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"ok":true}');
      return;
    }
    if (request.url === "/.well-known/agent-card.json") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"name":"test hive"}');
      return;
    }
    if (request.url?.startsWith("/work/subscribe?")) {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(": connected\n\n");
      return;
    }
    response.writeHead(404).end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.closeAllConnections());
  t.after(() => server.close());

  const address = server.address();
  const child = spawn(process.execPath, [memberScript], {
    env: { ...process.env, HIVE_URL: `http://127.0.0.1:${address.port}`, HIVE_AGENT_ID: "diagnostic-test" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  t.after(() => child.kill());

  const lines = readline.createInterface({ input: child.stdout });
  const waiters = new Map();
  lines.on("line", (line) => {
    const message = JSON.parse(line);
    const waiter = waiters.get(message.id);
    if (!waiter) return;
    waiters.delete(message.id);
    waiter(message);
  });
  const call = (id, method, params = {}) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiters.delete(id);
      reject(new Error(`timed out waiting for ${method}`));
    }, 5_000);
    waiters.set(id, (message) => { clearTimeout(timer); resolve(message); });
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  });

  await call(1, "initialize", { protocolVersion: "2025-03-26" });
  const response = await call(2, "tools/call", { name: "hive_diagnostics", arguments: { timeout_ms: 500 } });
  assert.equal(response.error, undefined);
  const result = JSON.parse(response.result.content[0].text);
  assert.equal(result.ok, true);
  assert.deepEqual(result.steps.map(({ step, ok }) => [step, ok]), [
    ["dns", true], ["tcp", true], ["http", true], ["work_sse", true],
  ]);
  assert.equal(result.endpoint, `127.0.0.1:${address.port}`);
});

test("initialize sends instructions and tool descriptions carry next-tool hints", async (t) => {
  const server = createServer((request, response) => {
    if (request.method === "POST" && request.url === "/agents/register") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"ok":true}');
      return;
    }
    response.writeHead(404).end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.closeAllConnections());
  t.after(() => server.close());

  const address = server.address();
  const child = spawn(process.execPath, [memberScript], {
    env: { ...process.env, HIVE_URL: `http://127.0.0.1:${address.port}`, HIVE_AGENT_ID: "instructions-test" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  t.after(() => child.kill());

  const lines = readline.createInterface({ input: child.stdout });
  const waiters = new Map();
  lines.on("line", (line) => {
    const message = JSON.parse(line);
    const waiter = waiters.get(message.id);
    if (!waiter) return;
    waiters.delete(message.id);
    waiter(message);
  });
  const call = (id, method, params = {}) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      waiters.delete(id);
      reject(new Error(`timed out waiting for ${method}`));
    }, 5_000);
    waiters.set(id, (message) => { clearTimeout(timer); resolve(message); });
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
  });

  const initialized = await call(1, "initialize", { protocolVersion: "2025-03-26" });
  assert.equal(typeof initialized.result.instructions, "string");
  assert.match(initialized.result.instructions, /generate_guidance_packet\(scope_path: "hive"\) against Steward/);
  assert.match(initialized.result.instructions, /before bidding \(hive_bid\)/);

  const listed = await call(2, "tools/list");
  const tools = listed.result.tools;
  assert.deepEqual(
    tools.map((tool) => tool.name).sort(),
    ["hive_agents", "hive_allocate", "hive_available_work", "hive_bid", "hive_complete", "hive_diagnostics", "hive_events", "hive_heartbeat", "hive_next_work", "hive_status", "hive_submit", "hive_wait", "hive_work_bids"],
  );
  for (const tool of tools) {
    assert.ok(tool.description.length > 40, `${tool.name} description lacks lifecycle guidance`);
    assert.match(tool.description, /hive_/, `${tool.name} description lacks a next-tool hint`);
  }
  const byName = new Map(tools.map((tool) => [tool.name, tool.description]));
  assert.equal(byName.get("hive_diagnostics"), "Check DNS, TCP, the Hive HTTP health route, and the worker SSE subscription from this container (opens and immediately closes an unclaimed stream; no work is consumed). Call this first when hive_wait or hive_next_work error or time out unexpectedly.");
  assert.equal(byName.get("hive_submit"), "Submit work to Hive and return its durable task id. After submitting, block on hive_wait(task_id) for the outcome — never poll hive_status.");
});

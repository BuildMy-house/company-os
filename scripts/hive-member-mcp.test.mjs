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

test("manager Hive tools prompt workers with the manager's own identity", async (t) => {
  const requests = [];
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    requests.push({ method: request.method, url: request.url, body: body ? JSON.parse(body) : null });
    response.writeHead(200, { "content-type": "application/json" });
    if (request.method === "POST" && request.url === "/agents/register") {
      response.end('{"ok":true}');
    } else if (request.url === "/work/human-readable-task/prompt") {
      response.end(JSON.stringify({ task_id: "task_123", prompted_agent_ids: ["worker-a"], already_bid_agent_ids: [] }));
    } else if (request.url === "/work/human-readable-task/bids?limit=100") {
      response.end(JSON.stringify({ work_id: "task_123", bids: [] }));
    } else {
      response.end("{}");
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.closeAllConnections());
  t.after(() => server.close());

  const address = server.address();
  const child = spawn(process.execPath, [memberScript], {
    env: { ...process.env, HIVE_URL: `http://127.0.0.1:${address.port}`, HIVE_AGENT_ID: "manager-self", AGENT_ROLE: "manager" },
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
  const listed = await call(2, "tools/list");
  const names = listed.result.tools.map(({ name }) => name);
  assert.ok(names.includes("hive_prompt_workers"));
  assert.ok(names.includes("hive_work_bids"));

  const prompted = await call(3, "tools/call", { name: "hive_prompt_workers", arguments: { work_slug: "human-readable-task" } });
  assert.deepEqual(JSON.parse(prompted.result.content[0].text).prompted_agent_ids, ["worker-a"]);
  const request = requests.find(({ url }) => url === "/work/human-readable-task/prompt");
  assert.deepEqual(request.body, { requester_id: "manager-self" });
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("hive_complete validates feedback client-side and forwards to Steward", async (t) => {
  const hive = { completeHits: 0 };
  const stewardCalls = [];
  const feedback = {
    calibration: { estimated_cost: 2, actual_cost: 3, estimated_benefit: 5, actual_benefit: 4 },
    friction: "",
    suggested_guidance_change: "",
  };

  const hiveServer = createServer((request, response) => {
    if (request.method === "POST" && request.url === "/agents/register") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"ok":true}');
      return;
    }
    if (request.method === "POST" && request.url?.startsWith("/work/") && request.url?.endsWith("/complete")) {
      hive.completeHits += 1;
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ id: "t1", state: "completed" }));
      return;
    }
    response.writeHead(404).end();
  });
  hiveServer.listen(0, "127.0.0.1");
  await once(hiveServer, "listening");
  t.after(() => hiveServer.closeAllConnections());
  t.after(() => hiveServer.close());

  const stewardServer = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => {
      const message = JSON.parse(body || "{}");
      if (message.method === "tools/call") stewardCalls.push(message.params);
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ jsonrpc: "2.0", id: message.id ?? 0, result: {} }));
    });
  });
  stewardServer.listen(0, "127.0.0.1");
  await once(stewardServer, "listening");
  t.after(() => stewardServer.closeAllConnections());
  t.after(() => stewardServer.close());

  const address = hiveServer.address();
  const child = spawn(process.execPath, [memberScript], {
    env: {
      ...process.env,
      HIVE_URL: `http://127.0.0.1:${address.port}`,
      HIVE_AGENT_ID: "feedback-test",
      STEWARD_MCP_URL: `http://127.0.0.1:${stewardServer.address().port}`,
    },
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
  const waitForSteward = async (name) => {
    for (let i = 0; i < 40 && !stewardCalls.some((call) => call.name === name); i += 1) await sleep(50);
    return stewardCalls.find((call) => call.name === name);
  };

  await call(1, "initialize", { protocolVersion: "2025-03-26" });

  const rejected = await call(2, "tools/call", { name: "hive_complete", arguments: { task_id: "t1", state: "completed", result: { ok: true } } });
  assert.equal(rejected.error?.code, -32000);
  assert.match(rejected.error.message, /result\.feedback/);
  await sleep(100);
  assert.equal(hive.completeHits, 0, "must reject client-side before any Hive HTTP");

  const withChange = await call(3, "tools/call", {
    name: "hive_complete",
    arguments: { task_id: "t1", state: "completed", result: { ok: true, feedback: { ...feedback, suggested_guidance_change: "Add lease-renewal example to hive_bid guidance" } } },
  });
  assert.equal(withChange.error, undefined);
  assert.equal(hive.completeHits, 1);
  const proposal = await waitForSteward("specs_propose");
  assert.ok(proposal, "specs_propose not forwarded");
  assert.equal(proposal.arguments.app, "hive");
  assert.equal(proposal.arguments.document_type, "knowledge");
  assert.match(proposal.arguments.path, /^hive_guidance\/t1-\d+$/);
  assert.match(proposal.arguments.content, /lease-renewal example/);

  const calibrationOnly = await call(4, "tools/call", {
    name: "hive_complete",
    arguments: { task_id: "t2", state: "completed", result: { ok: true, feedback: { ...feedback, friction: "bid guidance omitted the lease expiry rule" } } },
  });
  assert.equal(calibrationOnly.error, undefined);
  const report = await waitForSteward("submit_task_feedback");
  assert.ok(report, "submit_task_feedback not forwarded");
  assert.equal(report.arguments.agent_id, "feedback-test");
  assert.equal(report.arguments.task_id, "t2");
  assert.match(report.arguments.learned_for_agents, /lease expiry rule/);
  assert.equal(hive.completeHits, 2);
});

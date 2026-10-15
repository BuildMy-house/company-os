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
    ["hive_agents", "hive_allocate", "hive_available_work", "hive_bid", "hive_complete", "hive_cost_ledger", "hive_diagnostics", "hive_events", "hive_heartbeat", "hive_next_work", "hive_status", "hive_submit", "hive_wait", "hive_work_bids"],
  );
  for (const tool of tools) {
    assert.ok(tool.description.length > 40, `${tool.name} description lacks lifecycle guidance`);
    assert.match(tool.description, /hive_/, `${tool.name} description lacks a next-tool hint`);
  }
  const byName = new Map(tools.map((tool) => [tool.name, tool.description]));
  assert.equal(byName.get("hive_diagnostics"), "Check DNS, TCP, the Hive HTTP health route, and the worker SSE subscription from this container (opens and immediately closes an unclaimed stream; no work is consumed). Call this first when hive_wait or hive_next_work error or time out unexpectedly.");
  assert.match(byName.get("hive_submit"), /^Submit work to Hive and return its durable task id\. Tickets must follow docs\/TICKET-STANDARD\.md/);
  assert.match(byName.get("hive_submit"), /block on hive_wait\(task_id\) for the outcome — never poll hive_status\.$/);
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
    } else if (request.url === "/work/task_94119861ecab1150/bids?limit=100") {
      response.end(JSON.stringify({ work_id: "task_94119861ecab1150", bids: [] }));
    } else if (request.url === "/ledger?days=7&limit=5") {
      response.end(JSON.stringify({ agents: [], totals: {}, recent: [] }));
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

  // Regression: the manager schema used work_slug while the handler read work_id, so
  // every manager hive_work_bids call hit /work/undefined/bids ("work not found").
  assert.equal(names.filter((name) => name === "hive_work_bids").length, 1, "hive_work_bids must be listed once");
  for (const [id, args] of [[4, { work_slug: "human-readable-task" }], [5, { work_id: "human-readable-task" }], [6, { task_id: "task_94119861ecab1150" }]]) {
    const response = await call(id, "tools/call", { name: "hive_work_bids", arguments: args });
    assert.equal(response.error, undefined, JSON.stringify(response.error));
  }
  assert.ok(requests.some(({ url }) => url === "/work/task_94119861ecab1150/bids?limit=100"));
  assert.equal(requests.some(({ url }) => url.includes("/work/undefined/")), false);
  const missing = await call(7, "tools/call", { name: "hive_work_bids", arguments: {} });
  assert.ok(missing.error, "hive_work_bids without any work reference must fail fast");

  assert.ok(names.includes("hive_cost_ledger"));
  const ledger = await call(8, "tools/call", { name: "hive_cost_ledger", arguments: { days: 7, limit: 5 } });
  assert.deepEqual(JSON.parse(ledger.result.content[0].text), { agents: [], totals: {}, recent: [] });
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

test("hive_next_work failure logs a safe line and reports subscription failed", async (t) => {
  const server = createServer((request, response) => {
    if (request.method === "POST" && request.url === "/agents/register") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"ok":true}');
      return;
    }
    if (request.url?.startsWith("/work/subscribe?")) {
      response.writeHead(500, { "content-type": "text/plain" });
      response.end("hive coordinator restarting");
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
    env: { ...process.env, HIVE_URL: `http://127.0.0.1:${address.port}`, HIVE_AGENT_ID: "work-failure-test" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  t.after(() => child.kill());

  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const notifications = [];
  const lines = readline.createInterface({ input: child.stdout });
  const waiters = new Map();
  lines.on("line", (line) => {
    const message = JSON.parse(line);
    if (message.method === "notifications/message") { notifications.push(message.params.data); return; }
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
  const waitFor = async (fn) => {
    for (let i = 0; i < 100 && !fn(); i += 1) await sleep(20);
  };

  await call(1, "initialize", { protocolVersion: "2025-03-26" });
  const response = await call(2, "tools/call", { name: "hive_next_work", arguments: { timeout_seconds: 5 } });
  assert.ok(response.error, "hive_next_work must surface the stream failure");
  const reply = JSON.parse(response.error.message);
  assert.equal(reply.stage, "hive_next_work");
  assert.equal(reply.http_status, 500);

  const failed = notifications.find((data) => data.state === "failed");
  assert.ok(failed, `no failed subscription notification, got ${JSON.stringify(notifications)}`);
  assert.equal(failed.event, "hive_subscription");
  assert.equal(failed.agent_id, "work-failure-test");
  assert.equal(failed.stage, "hive_next_work");
  assert.equal(failed.message, "Hive work stream returned 500");

  await waitFor(() => stderr.includes("[hive-member] hive_next_work failed: {"));
  const logged = stderr.match(/\[hive-member\] hive_next_work failed: (.+)/)?.[1];
  assert.ok(logged, `missing stderr line: ${stderr}`);
  const details = JSON.parse(logged);
  assert.equal(details.stage, "hive_next_work");
  assert.equal(details.http_status, 500);
  assert.equal(logged.split("\n").length, 1, "stderr failure line must be single-line JSON");
});

test("hive_next_work returns null on long-poll timeout without a failed report", async (t) => {
  const server = createServer((request, response) => {
    if (request.method === "POST" && request.url === "/agents/register") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"ok":true}');
      return;
    }
    if (request.url?.startsWith("/work/subscribe?")) {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(": connected\n\n");
      return; // stream stays open; the client's own timeout must end it cleanly
    }
    response.writeHead(404).end();
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.closeAllConnections());
  t.after(() => server.close());

  const address = server.address();
  const child = spawn(process.execPath, [memberScript], {
    env: { ...process.env, HIVE_URL: `http://127.0.0.1:${address.port}`, HIVE_AGENT_ID: "work-timeout-test" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  t.after(() => child.kill());

  const notifications = [];
  const lines = readline.createInterface({ input: child.stdout });
  const waiters = new Map();
  lines.on("line", (line) => {
    const message = JSON.parse(line);
    if (message.method === "notifications/message") { notifications.push(message.params.data); return; }
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
  const response = await call(2, "tools/call", { name: "hive_next_work", arguments: { timeout_seconds: 1 } });
  assert.equal(response.error, undefined, JSON.stringify(response.error));
  assert.equal(JSON.parse(response.result.content[0].text), null, "timeout must return null, not an error");
  assert.ok(notifications.some((data) => data.state === "connected"));
  assert.equal(notifications.some((data) => data.state === "failed"), false, "timeout is idle, not a failure");
});

test("HIVE_REGISTER=false never calls /agents/register, default still registers", async (t) => {
  async function registrations(env) {
    const paths = [];
    const server = createServer((request, response) => {
      paths.push(request.url);
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"ok":true}');
    });
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    const child = spawn(process.execPath, [memberScript], {
      env: { ...process.env, HIVE_URL: `http://127.0.0.1:${server.address().port}`, HIVE_AGENT_ID: "register-test", ...env },
      stdio: ["pipe", "pipe", "inherit"],
    });
    await new Promise((resolve) => setTimeout(resolve, 700));
    child.kill();
    server.closeAllConnections();
    server.close();
    return paths.filter((path) => path === "/agents/register").length;
  }
  assert.equal(await registrations({ HIVE_REGISTER: "false" }), 0);
  assert.equal(await registrations({}), 1);
});

test("hive_submit enforces the ticket standard and derives tier from points", async (t) => {
  const submitted = [];
  const server = createServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    if (request.method === "POST" && request.url === "/") submitted.push(JSON.parse(body));
    response.writeHead(200, { "content-type": "application/json" });
    response.end('{"result":{"id":"task_test"}}');
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => server.closeAllConnections());
  t.after(() => server.close());

  const child = spawn(process.execPath, [memberScript], {
    env: { ...process.env, HIVE_URL: `http://127.0.0.1:${server.address().port}`, HIVE_AGENT_ID: "ticket-test", HIVE_REGISTER: "false" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  t.after(() => child.kill());
  const lines = readline.createInterface({ input: child.stdout });
  const waiters = new Map();
  lines.on("line", (line) => {
    const message = JSON.parse(line);
    waiters.get(message.id)?.(message);
    waiters.delete(message.id);
  });
  let nextId = 1;
  const submit = (args) => new Promise((resolve) => {
    const id = nextId++;
    waiters.set(id, resolve);
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call", params: { name: "hive_submit", arguments: args } })}\n`);
  });
  const ticket = { type: "feature", points: 3, goal: "Add the thing.", acceptance_criteria: ["tests pass"], scope: "repo app, src/ only" };

  const missing = await submit({ message: "do it", metadata: { tier: "cheap" } });
  assert.match(missing.error.message, /TICKET-STANDARD/);
  assert.match(missing.error.message, /metadata\.points/);
  assert.equal(submitted.length, 0, "a malformed ticket never reaches Hive");

  const tooBig = await submit({ message: "do it", metadata: { ...ticket, type: "bug", points: 13, bug_modifiers: ["needs_diagnosis"] } });
  assert.match(tooBig.error.message, /split/);

  await submit({ message: "do it", metadata: ticket });
  assert.equal(submitted[0].params.metadata.tier, "cheap");
  assert.equal(submitted[0].params.metadata.points, 3);
  assert.match(submitted[0].params.message.parts[0].text, /Acceptance criteria:\n- tests pass/);

  await submit({ message: "fix it", metadata: { ...ticket, type: "bug", points: 3, bug_modifiers: ["needs_diagnosis", "touches_production"] } });
  assert.equal(submitted[1].params.metadata.points, 6);
  assert.equal(submitted[1].params.metadata.base_points, 3);
  assert.equal(submitted[1].params.metadata.tier, "power");

  await submit({ message: "five", metadata: { ...ticket, points: 5 } });
  assert.equal(submitted[2].params.metadata.tier, "standard");
});

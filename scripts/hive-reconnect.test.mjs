// Regression tests for the 2026-10-25 incident: after hive-coordinator restarted, every worker
// logged five "hive subscription failure" lines and then never reconnected, never heartbeated,
// and stayed Running/Ready for ~40h.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import readline from "node:readline";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { subscriptionBackoffMs, runSubscriptionLoop, hiveHealth } from "./engineering-manager-mcp.js";

const memberScript = fileURLToPath(new URL("./hive-member-mcp.js", import.meta.url));

function startMember(t, env) {
  const child = spawn(process.execPath, [memberScript], { env: { ...process.env, ...env }, stdio: ["pipe", "pipe", "ignore"] });
  t.after(() => child.kill());
  const waiters = new Map();
  readline.createInterface({ input: child.stdout }).on("line", (line) => {
    const message = JSON.parse(line);
    const waiter = waiters.get(message.id);
    if (!waiter) return;
    waiters.delete(message.id);
    waiter(message);
  });
  let id = 0;
  const call = (name, args = {}, timeoutMs = 10_000) => new Promise((resolve, reject) => {
    const callId = ++id;
    const timer = setTimeout(() => { waiters.delete(callId); reject(new Error(`timed out waiting for ${name}`)); }, timeoutMs);
    waiters.set(callId, (message) => { clearTimeout(timer); resolve(message); });
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: callId, method: "tools/call", params: { name, arguments: args } })}\n`);
  });
  return { call };
}

async function freePort() {
  const probe = createServer();
  probe.listen(0, "127.0.0.1");
  await once(probe, "listening");
  const { port } = probe.address();
  probe.close();
  await once(probe, "close");
  return port;
}

test("subscriptionBackoffMs is capped at 30s, jittered, and never below half the ceiling", () => {
  for (let attempt = 1; attempt <= 40; attempt += 1) {
    const ceiling = Math.min(30_000, 1_000 * 2 ** (attempt - 1));
    for (const random of [() => 0, () => 0.5, () => 0.999999]) {
      const delay = subscriptionBackoffMs(attempt, random);
      assert.ok(delay >= Math.floor(ceiling / 2) && delay <= ceiling, `attempt ${attempt}: ${delay} outside [${ceiling / 2}, ${ceiling}]`);
      assert.ok(delay <= 30_000);
    }
  }
});

test("subscription loop survives far more than 5 failures and resumes when Hive recovers", async () => {
  const delays = [];
  const failures = [];
  const events = [];
  let polls = 0;
  let okMarks = 0;
  const worked = [];
  let stop = false;
  const call = async () => {
    polls += 1;
    if (polls <= 12) throw Object.assign(new Error("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    return { id: "work-1" };
  };
  await runSubscriptionLoop({
    call,
    runWork: async (candidate) => { worked.push(candidate.id); stop = true; },
    sleep: async (ms) => { delays.push(ms); },
    log: (line) => failures.push(line),
    emit: (event) => events.push(event),
    markOk: () => { okMarks += 1; },
    random: () => 0.5,
    shouldStop: () => stop,
  });
  assert.equal(failures.length, 12, "every failure is logged, not just the first five");
  assert.match(failures[11], /attempt=12 /);
  assert.equal(delays.length, 12);
  assert.ok(delays.every((ms) => ms <= 30_000));
  assert.deepEqual(worked, ["work-1"], "work is processed after the coordinator comes back");
  assert.equal(okMarks, 1);
  assert.ok(events.some((e) => e.state === "reconnected" && e.attempts === 12));
});

test("subscription loop keeps going when telemetry, logging, or work handling throws", async () => {
  let polls = 0;
  let stop = false;
  await runSubscriptionLoop({
    call: async () => { polls += 1; return polls >= 4 ? { id: "ok" } : null; },
    runWork: async () => { if (polls === 4) throw new Error("work blew up"); stop = polls >= 6; },
    sleep: async () => {},
    log: () => { throw new Error("log blew up"); },
    emit: () => { throw new Error("emit blew up"); },
    shouldStop: () => stop,
  });
  assert.ok(polls >= 6);
});

test("hiveHealth: healthy while recently ok or busy, unhealthy once stale, always healthy without Hive", () => {
  const base = { now: 1_000_000, staleMs: 600_000, enabled: true, busy: false };
  assert.equal(hiveHealth({ ...base, lastOkAt: 1_000_000 - 599_000 }).ok, true);
  assert.equal(hiveHealth({ ...base, lastOkAt: 1_000_000 - 601_000 }).ok, false);
  assert.equal(hiveHealth({ ...base, lastOkAt: 0, busy: true }).ok, true);
  assert.equal(hiveHealth({ ...base, lastOkAt: 0, enabled: false }).ok, true);
});

test("member resumes after the coordinator is down for more than 5 attempts and re-registers", async (t) => {
  const port = await freePort();
  const { call } = startMember(t, {
    HIVE_URL: `http://127.0.0.1:${port}`,
    HIVE_AGENT_ID: "reconnect-test",
    HIVE_REGISTER_HEARTBEAT_MS: "200",
  });

  // Coordinator is down: well over 5 consecutive failures, each must still fail fast and cleanly.
  for (let attempt = 1; attempt <= 7; attempt += 1) {
    const response = await call("hive_next_work", { timeout_seconds: 5 });
    assert.ok(response.error, `attempt ${attempt} should fail while Hive is down`);
    assert.match(response.error.message, /ECONNREFUSED/);
  }

  // Coordinator comes back (fresh instance: no memory of the agent) and offers work.
  const registrations = [];
  const hive = createServer((request, response) => {
    if (request.method === "POST" && request.url === "/agents/register") {
      let body = "";
      request.on("data", (chunk) => { body += chunk; });
      request.on("end", () => { registrations.push(JSON.parse(body).id); response.writeHead(200, { "content-type": "application/json" }); response.end('{"ok":true}'); });
      return;
    }
    if (request.url?.startsWith("/work/subscribe?")) {
      response.writeHead(200, { "content-type": "text/event-stream" });
      response.write(": heartbeat\n\n");
      response.write(`data: ${JSON.stringify({ id: "work-after-restart", slug: "w" })}\n\n`);
      return;
    }
    response.writeHead(404).end();
  });
  hive.listen(port, "127.0.0.1");
  await once(hive, "listening");
  t.after(() => hive.closeAllConnections());
  t.after(() => hive.close());

  const response = await call("hive_next_work", { timeout_seconds: 5 });
  assert.equal(response.error, undefined);
  assert.equal(JSON.parse(response.result.content[0].text).id, "work-after-restart");

  // The member re-registered by itself (background retry and/or heartbeat) without a restart.
  const deadline = Date.now() + 10_000;
  while (registrations.length === 0 && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 100));
  assert.ok(registrations.includes("reconnect-test"), "member must re-register with the restarted coordinator");
});

test("member treats a silent SSE stream as dead and fails fast instead of hanging", async (t) => {
  const hive = createServer((request, response) => {
    if (request.method === "POST" && request.url === "/agents/register") { response.writeHead(200, { "content-type": "application/json" }); response.end("{}"); return; }
    if (request.url?.startsWith("/work/subscribe?")) { response.writeHead(200, { "content-type": "text/event-stream" }); response.write(": connected\n\n"); return; } // then silence
    response.writeHead(404).end();
  });
  hive.listen(0, "127.0.0.1");
  await once(hive, "listening");
  t.after(() => hive.closeAllConnections());
  t.after(() => hive.close());
  const { call } = startMember(t, { HIVE_URL: `http://127.0.0.1:${hive.address().port}`, HIVE_AGENT_ID: "stall-test", HIVE_STREAM_IDLE_MS: "300" });
  const started = Date.now();
  const response = await call("hive_next_work", { timeout_seconds: 60 }, 8_000);
  assert.ok(response.error, "stalled stream must surface as an error so the loop re-dials");
  assert.match(response.error.message, /silent for 300ms/);
  assert.ok(Date.now() - started < 5_000);
});

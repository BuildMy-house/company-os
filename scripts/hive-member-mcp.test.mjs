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

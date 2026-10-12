import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createGateway, redact } from "./mcp-gateway.js";

const dir = mkdtempSync(join(tmpdir(), "gw-"));
const child = join(dir, "child.mjs");
writeFileSync(child, `
import readline from "node:readline";
const tools = ["echo","secret","forbidden","die"].map((name) => ({ name, inputSchema: { type: "object" } }));
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.id === undefined) return;
  const send = (result) => console.log(JSON.stringify({ jsonrpc: "2.0", id: m.id, result }));
  if (m.method === "initialize") return send({ protocolVersion: "2025-03-26", capabilities: {} });
  if (m.method === "tools/list") return send({ tools });
  if (m.params.name === "die") process.exit(1);
  if (m.params.name === "secret") return send({ content: [{ type: "text", text: "tok Bearer abcdefghijklmnopqrstuvwxyz0123 end" }] });
  send({ content: [{ type: "text", text: "agent=" + process.env.HIVE_AGENT_ID + " " + JSON.stringify(m.params.arguments) }] });
});
`);
writeFileSync(join(dir, "a.tok"), "token-a\n");
writeFileSync(join(dir, "b.tok"), "token-b\n");
const policy = {
  servers: { demo: { command: ["node", child], redactTools: ["secret"], longPollTools: [] } },
  clients: {
    a: { tokenFile: join(dir, "a.tok"), servers: { demo: { tools: ["echo", "secret", "die"], agentId: "agent-a", argLimits: { echo: { n: { max: 5 } } } } } },
    b: { tokenFile: join(dir, "b.tok"), servers: {} },
  },
};

const gw = createGateway({ policy, env: {} });
await new Promise((r) => gw.server.listen(0, r));
const base = `http://127.0.0.1:${gw.server.address().port}`;
test.after(() => gw.close());

const rpc = (token, method, params, path = "/demo") =>
  fetch(base + path, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });

test("auth: missing/wrong token 401, identity without server 403, unknown path 404", async () => {
  assert.equal((await fetch(base + "/demo", { method: "POST", body: "{}" })).status, 401);
  assert.equal((await rpc("nope", "ping")).status, 401);
  assert.equal((await rpc("token-b", "ping")).status, 403);
  assert.equal((await rpc("token-a", "ping", {}, "/other")).status, 404);
});

test("stateless: no session header, GET rejected", async () => {
  const res = await rpc("token-a", "initialize", {});
  assert.equal(res.headers.get("mcp-session-id"), null);
  assert.equal((await fetch(base + "/demo", { headers: { authorization: "Bearer token-a" } })).status, 405);
});

test("tools/list is filtered by allowlist; tools/call outside it is denied", async () => {
  const list = await (await rpc("token-a", "tools/list", {})).json();
  assert.deepEqual(list.result.tools.map((t) => t.name).sort(), ["die", "echo", "secret"]);
  const denied = await (await rpc("token-a", "tools/call", { name: "forbidden", arguments: {} })).json();
  assert.equal(denied.error.code, -32001);
});

test("per-identity env and argument limits", async () => {
  const ok = await (await rpc("token-a", "tools/call", { name: "echo", arguments: { n: 3 } })).json();
  assert.match(ok.result.content[0].text, /agent=agent-a/);
  const over = await (await rpc("token-a", "tools/call", { name: "echo", arguments: { n: 9 } })).json();
  assert.equal(over.error.code, -32001);
});

test("redaction of configured tools", async () => {
  const res = await (await rpc("token-a", "tools/call", { name: "secret", arguments: {} })).json();
  assert.ok(!res.result.content[0].text.includes("abcdefghijklmnop"));
  assert.match(res.result.content[0].text, /\[REDACTED\]/);
  assert.equal(redact("ghp_" + "a".repeat(30)), "[REDACTED]");
});

test("child crash returns an error then the child is respawned", async () => {
  const crashed = await (await rpc("token-a", "tools/call", { name: "die", arguments: {} })).json();
  assert.ok(crashed.error);
  const deadline = Date.now() + 8000;
  let ok;
  while (Date.now() < deadline) {
    ok = await (await rpc("token-a", "tools/call", { name: "echo", arguments: {} })).json();
    if (ok.result) break;
    await new Promise((r) => setTimeout(r, 300));
  }
  assert.ok(ok.result, "child should recover");
});

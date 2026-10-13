// Integration tests for gateway key management: HTTP auth with issued keys, the
// in-process gateway-admin tools, migration off file tokens, fail-closed behaviour and
// log hygiene. Uses the in-memory key store and a fake child; no network, no database.
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { createGateway, loadPolicy, ADMIN_SERVER } from "./mcp-gateway.js";
import { createMemoryKeyStore, KEY_PREFIX } from "./mcp-gateway-keys.js";

const logLines = [];
const realLog = console.log;
console.log = (...args) => { logLines.push(args.join(" ")); };
test.after(() => { console.log = realLog; });

const dir = mkdtempSync(join(tmpdir(), "gw-admin-"));
const child = join(dir, "child.mjs");
writeFileSync(child, `
import readline from "node:readline";
const tools = ["echo", "other"].map((name) => ({ name, inputSchema: { type: "object" } }));
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.id === undefined) return;
  const send = (result) => console.log(JSON.stringify({ jsonrpc: "2.0", id: m.id, result }));
  if (m.method === "initialize") return send({ protocolVersion: "2025-03-26", capabilities: {} });
  if (m.method === "tools/list") return send({ tools });
  send({ content: [{ type: "text", text: "ran " + m.params.name }] });
});
`);
const ADMIN_TOKEN = "admin-bootstrap-token-" + "a".repeat(20);
const LEGACY_TOKEN = "f".repeat(64); // 64-hex like `openssl rand -hex 32`: must never leak into logs
writeFileSync(join(dir, "admin.tok"), ADMIN_TOKEN + "\n");
writeFileSync(join(dir, "legacy.tok"), LEGACY_TOKEN + "\n");

const policy = {
  servers: { demo: { command: ["node", child] }, other: { command: ["node", child] } },
  clients: {
    "gateway-admin": { admin: true, tokenFile: join(dir, "admin.tok") },
    legacy: { tokenFile: join(dir, "legacy.tok"), keyTtlDays: 90, servers: { demo: { tools: ["echo"] } } },
    deploy: { keyTtlDays: 30, servers: { other: { tools: ["echo"], argLimits: { echo: { n: { max: 2 } } } } } },
  },
};

let clock = new Date("2026-10-13T00:00:00Z");
const store = createMemoryKeyStore();
const gw = createGateway({ policy, env: {}, keyStore: store, now: () => clock });
await new Promise((r) => gw.server.listen(0, r));
const base = `http://127.0.0.1:${gw.server.address().port}`;
test.after(() => gw.close());

const post = (token, path, method, params) =>
  fetch(base + path, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
async function admin(tool, args = {}) {
  const res = await post(ADMIN_TOKEN, "/" + ADMIN_SERVER, "tools/call", { name: tool, arguments: args });
  assert.equal(res.status, 200);
  const body = await res.json();
  return body.error ? { error: body.error } : JSON.parse(body.result.content[0].text);
}
const call = (token, server, args = {}) => post(token, "/" + server, "tools/call", { name: "echo", arguments: args });
const sha = (s) => createHash("sha256").update(s).digest("hex");

test("policy loading: real manifest loads; admin and keys-only rules enforced", () => {
  const manifest = readFileSync(fileURLToPath(new URL("../k8s/base/mcp-gateway/mcp-gateway.yaml", import.meta.url)), "utf8");
  const doc = manifest.split(/^---\n/m).find((d) => d.includes("kind: ConfigMap") && /^  name: mcp-gateway-policy$/m.test(d));
  const lines = doc.split("\n");
  const block = [];
  for (const line of lines.slice(lines.indexOf("  policy.json: |") + 1)) { if (line === "" || line.startsWith("    ")) block.push(line.slice(4)); else break; }
  const file = join(dir, "real-policy.json");
  writeFileSync(file, block.join("\n"));
  const real = loadPolicy(file);
  assert.equal(real.clients["gateway-admin"].admin, true);
  assert.equal(real.clients["local-claude-deploy"].tokenFile, undefined);

  const write = (obj) => { const f = join(dir, "p.json"); writeFileSync(f, JSON.stringify(obj)); return f; };
  const servers = { demo: { command: ["node"] } };
  assert.throws(() => loadPolicy(write({ servers: { [ADMIN_SERVER]: { command: ["node"] } }, clients: {} })), /reserved/);
  assert.throws(() => loadPolicy(write({ servers, clients: { a: { admin: true } } })), /requires tokenFile/);
  assert.throws(() => loadPolicy(write({ servers, clients: { a: { admin: true, tokenFile: "/x", servers: { demo: { tools: [] } } } } })), /cannot be granted child servers/);
  assert.throws(() => loadPolicy(write({ servers, clients: { a: { keyTtlDays: 0, servers: {} } } })), /keyTtlDays/);
  assert.doesNotThrow(() => loadPolicy(write({ servers, clients: { a: { servers: { demo: { tools: ["x"] } } } } })));
});

test("admin-only: only the admin identity reaches /gateway-admin; admin reaches no child server", async () => {
  assert.equal((await fetch(base + "/gateway-admin", { method: "POST", body: "{}" })).status, 401);
  assert.equal((await post(LEGACY_TOKEN, "/gateway-admin", "tools/list", {})).status, 403, "file-token non-admin");
  const issued = await admin("issue_key", { identity: "deploy", label: "t1" });
  assert.equal((await post(issued.plaintext, "/gateway-admin", "tools/list", {})).status, 403, "issued key is never admin");
  const list = await (await post(ADMIN_TOKEN, "/gateway-admin", "tools/list", {})).json();
  assert.deepEqual(list.result.tools.map((t) => t.name).sort(), ["issue_key", "list_keys", "revoke_all_for_identity", "revoke_key", "rotate_key"]);
  assert.equal((await post(ADMIN_TOKEN, "/demo", "tools/list", {})).status, 403, "admin has no child servers");
  const unknown = await (await post(ADMIN_TOKEN, "/gateway-admin", "tools/call", { name: "exec", arguments: {} })).json();
  assert.equal(unknown.error.code, -32001);
  await admin("revoke_all_for_identity", { identity: "deploy" });
});

test("issue: plaintext once, scope-bound to one identity and its policy, TTLs per identity", async () => {
  const k = await admin("issue_key", { identity: "deploy", label: "ttl30" });
  assert.ok(k.plaintext.startsWith(KEY_PREFIX));
  assert.equal(new Date(k.expires_at).getTime() - clock.getTime(), 30 * 86_400_000);
  const l = await admin("issue_key", { identity: "legacy", label: "ttl90" });
  assert.equal(new Date(l.expires_at).getTime() - clock.getTime(), 90 * 86_400_000);
  const listing = JSON.stringify(await admin("list_keys", {}));
  assert.ok(!listing.includes(k.plaintext) && !listing.includes(sha(k.plaintext)), "list never shows plaintext or hash");
  assert.match(listing, new RegExp(k.fingerprint));
  assert.ok((await admin("issue_key", { identity: "deploy", label: "x", ttl_days: 31 })).error, "cannot extend TTL");
  assert.ok((await admin("issue_key", { identity: "gateway-admin", label: "x" })).error, "admin identity never key-issuable");
  assert.ok((await admin("issue_key", { identity: "nobody", label: "x" })).error);
  assert.ok((await admin("issue_key", { identity: "deploy", label: "x", extra: 1 })).error, "unknown argument rejected");
  // allowlist/argLimits come from the policy, not the key
  assert.equal((await (await call(k.plaintext, "other", { n: 1 })).json()).result.content[0].text, "ran echo");
  assert.equal((await (await call(k.plaintext, "other", { n: 9 })).json()).error.code, -32001, "argLimit still applies to a key");
  const denied = await (await post(k.plaintext, "/other", "tools/call", { name: "other", arguments: {} })).json();
  assert.equal(denied.error.code, -32001, "tool outside the identity allowlist");
  assert.equal((await call(k.plaintext, "demo")).status, 403, "deploy key cannot reach legacy's server");
  assert.equal((await call(l.plaintext, "other")).status, 403, "legacy key cannot reach deploy's server");
});

test("expiry and revocation fail immediately", async () => {
  const k = await admin("issue_key", { identity: "deploy", label: "exp", ttl_days: 1 });
  assert.equal((await call(k.plaintext, "other")).status, 200);
  clock = new Date(clock.getTime() + 86_400_000);
  assert.equal((await call(k.plaintext, "other")).status, 401, "expired");
  const r = await admin("issue_key", { identity: "deploy", label: "rev" });
  assert.equal((await call(r.plaintext, "other")).status, 200);
  await admin("revoke_key", { key_id: r.id });
  assert.equal((await call(r.plaintext, "other")).status, 401, "revoked");
  const a = await admin("issue_key", { identity: "deploy", label: "all1" });
  const b = await admin("issue_key", { identity: "deploy", label: "all2" });
  assert.ok((await admin("revoke_all_for_identity", { identity: "deploy" })).revoked >= 2);
  assert.equal((await call(a.plaintext, "other")).status, 401);
  assert.equal((await call(b.plaintext, "other")).status, 401);
  assert.ok((await admin("revoke_all_for_identity", { identity: "gateway-admin" })).error);
});

test("rotation: overlap window, then only the new key works", async () => {
  const old = await admin("issue_key", { identity: "deploy", label: "rot" });
  const next = await admin("rotate_key", { key_id: old.id, overlap_seconds: 600 });
  assert.notEqual(next.plaintext, old.plaintext);
  assert.equal((await call(old.plaintext, "other")).status, 200, "old key valid during overlap");
  assert.equal((await call(next.plaintext, "other")).status, 200);
  clock = new Date(clock.getTime() + 601_000);
  assert.equal((await call(old.plaintext, "other")).status, 401, "old key gone after overlap");
  assert.equal((await call(next.plaintext, "other")).status, 200);
  assert.ok((await admin("rotate_key", { key_id: "not-a-uuid" })).error);
});

test("migration: file token works until the identity has an issued key, then never again", async () => {
  const fresh = createMemoryKeyStore();
  const g = createGateway({ policy, env: {}, keyStore: fresh, now: () => clock });
  await new Promise((r) => g.server.listen(0, r));
  const b = `http://127.0.0.1:${g.server.address().port}`;
  const go = (token, path, method, params) => fetch(b + path, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  try {
    assert.equal((await go(LEGACY_TOKEN, "/demo", "tools/call", { name: "echo", arguments: {} })).status, 200);
    const issue = await (await go(ADMIN_TOKEN, "/gateway-admin", "tools/call", { name: "issue_key", arguments: { identity: "legacy", label: "m" } })).json();
    const key = JSON.parse(issue.result.content[0].text);
    assert.equal((await go(LEGACY_TOKEN, "/demo", "tools/call", { name: "echo", arguments: {} })).status, 401, "file token retired");
    assert.equal((await go(key.plaintext, "/demo", "tools/call", { name: "echo", arguments: {} })).status, 200);
    await go(ADMIN_TOKEN, "/gateway-admin", "tools/call", { name: "revoke_all_for_identity", arguments: { identity: "legacy" } });
    assert.equal((await go(LEGACY_TOKEN, "/demo", "tools/call", { name: "echo", arguments: {} })).status, 401, "revoke-all does not fall back to the file token");
  } finally { g.close(); }
});

test("fail closed when the key store is down; admin tools report an unconfigured store", async () => {
  const broken = new Proxy(createMemoryKeyStore(), { get: (t, p) => (p === "then" ? undefined : async () => { throw new Error("db down"); }) });
  const g = createGateway({ policy, env: {}, keyStore: broken, now: () => clock });
  await new Promise((r) => g.server.listen(0, r));
  const b = `http://127.0.0.1:${g.server.address().port}`;
  const go = (token, path) => fetch(b + path, { method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "echo", arguments: {} } }) });
  try {
    assert.equal((await go(KEY_PREFIX + "x".repeat(43), "/other")).status, 503);
    assert.equal((await go(LEGACY_TOKEN, "/demo")).status, 503, "file-token migration state unknown => deny");
  } finally { g.close(); }
  const bare = createGateway({ policy, env: {}, now: () => clock });
  await new Promise((r) => bare.server.listen(0, r));
  const bb = `http://127.0.0.1:${bare.server.address().port}`;
  try {
    const res = await (await fetch(bb + "/gateway-admin", { method: "POST", headers: { authorization: `Bearer ${ADMIN_TOKEN}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "list_keys", arguments: {} } }) })).json();
    assert.match(res.error.message, /not configured/);
    assert.equal((await fetch(bb + "/demo", { method: "POST", headers: { authorization: `Bearer ${LEGACY_TOKEN}`, "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }) })).status, 200, "file-token-only mode keeps working");
  } finally { bare.close(); }
});

test("audit hygiene: lifecycle events present, fingerprints only, no plaintext or hash anywhere", async () => {
  const k = await admin("issue_key", { identity: "deploy", label: "audit" });
  await call(KEY_PREFIX + "bogus".repeat(10), "other");
  await call(LEGACY_TOKEN.replace(/f/g, "e"), "demo"); // wrong 64-hex token
  await admin("rotate_key", { key_id: k.id, overlap_seconds: 0 });
  const joined = logLines.join("\n");
  const events = new Set(logLines.map((l) => { try { return JSON.parse(l).event; } catch { return null; } }));
  for (const e of ["key_issued", "key_rotated", "key_revoked", "keys_revoked_all", "auth_failure"]) assert.ok(events.has(e), `audit event ${e}`);
  assert.ok(joined.includes(k.fingerprint) && k.fingerprint.length === 8);
  assert.ok(!joined.includes(k.plaintext), "plaintext never logged");
  assert.ok(!joined.includes(sha(k.plaintext)), "full hash never logged");
  assert.ok(!joined.includes(ADMIN_TOKEN) && !joined.includes(LEGACY_TOKEN));
  const wrongHex = LEGACY_TOKEN.replace(/f/g, "e");
  assert.ok(!joined.includes(wrongHex.slice(0, 8)), "a 64-hex presented token must not leak its own prefix as a fingerprint");
  const failures = logLines.map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter((r) => r?.event === "auth_failure");
  assert.ok(failures.every((f) => f.token_fingerprint === null || /^[0-9a-f]{8}$/.test(f.token_fingerprint)));
  assert.ok(failures.some((f) => f.reason === "expired") && failures.some((f) => f.reason === "revoked") && failures.some((f) => f.reason === "file_token_retired" || f.reason === "unknown"));
});

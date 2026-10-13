import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createGateway, redact, violatesLimits } from "./mcp-gateway.js";

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

// Manifest tests: parse the policy JSON out of the ConfigMap in
// k8s/base/mcp-gateway/mcp-gateway.yaml so the
// deployed allowlist cannot silently drift from what the review decisions require.
const manifest = readFileSync(
  fileURLToPath(new URL("../k8s/base/mcp-gateway/mcp-gateway.yaml", import.meta.url)),
  "utf8",
);

function configMapPolicy(source, name) {
  for (const doc of source.split(/^---\n/m)) {
    if (!doc.includes("kind: ConfigMap") || !new RegExp(`^  name: ${name}$`, "m").test(doc)) continue;
    const lines = doc.split("\n");
    const start = lines.indexOf("  policy.json: |");
    assert.ok(start !== -1, `policy.json block not found in ConfigMap ${name}`);
    const block = [];
    for (const line of lines.slice(start + 1)) {
      if (line === "" || line.startsWith("    ")) block.push(line.slice(4));
      else break;
    }
    return JSON.parse(block.join("\n"));
  }
  assert.fail(`ConfigMap ${name} not found in k8s/base/mcp-gateway/mcp-gateway.yaml`);
}

test("manifest policy: no client lists hive bid/complete/heartbeat/next_work/allocate tools", () => {
  const policy = configMapPolicy(manifest, "mcp-gateway-policy");
  const flat = JSON.stringify(policy);
  for (const tool of ["hive_bid", "hive_complete", "hive_heartbeat", "hive_next_work", "hive_allocate", "allocate"]) {
    assert.ok(!flat.includes(tool), `policy must not mention ${tool}`);
  }
  assert.ok(!policy.servers.hive.longPollTools.includes("hive_next_work"), "hive_next_work must not be a long poll tool");
  assert.ok(policy.servers.hive.longPollTools.includes("hive_wait"), "hive_wait must stay a long poll tool");
  const hiveAllowed = ["hive_agents", "hive_available_work", "hive_diagnostics", "hive_events", "hive_status", "hive_submit", "hive_wait", "hive_work_bids"];
  for (const [id, client] of Object.entries(policy.clients)) {
    if (id === "local-claude-deploy") {
      assert.equal(client.servers.hive, undefined, "local-claude-deploy must not reach hive at all");
      continue;
    }
    assert.deepEqual(
      [...client.servers.hive.tools].sort(),
      hiveAllowed,
      `client ${id} hive allowlist must be exactly the read tools + hive_submit`,
    );
  }
});

test("manifest policy: three clients with distinct agentIds and tokenFiles", () => {
  const policy = configMapPolicy(manifest, "mcp-gateway-policy");
  const trio = ["local-claude", "local-codex", "local-opencode"];
  const ids = Object.keys(policy.clients);
  assert.equal(ids.length, 4);
  assert.deepEqual([...ids].sort(), ["local-claude", "local-claude-deploy", "local-codex", "local-opencode"]);
  const agentIds = new Set();
  const tokenFiles = new Set();
  for (const id of trio) {
    const client = policy.clients[id];
    assert.equal(client.servers.hive.agentId, id, `agentId must equal identity name ${id}`);
    assert.equal(client.tokenFile, `/etc/mcp-gateway/tokens/${id.toUpperCase().replaceAll("-", "_")}_TOKEN`);
    agentIds.add(client.servers.hive.agentId);
    tokenFiles.add(client.tokenFile);
  }
  assert.equal(agentIds.size, 3, "agentIds must be distinct");
  assert.equal(tokenFiles.size, 3, "tokenFiles must be distinct");
  const reference = policy.clients["local-claude"].servers;
  assert.deepEqual(Object.keys(reference).sort(), ["hive", "kubernetes"]);
  for (const server of Object.keys(reference)) {
    for (const id of trio) {
      assert.deepEqual(
        [...policy.clients[id].servers[server].tools].sort(),
        [...reference[server].tools].sort(),
        `client ${id} ${server} allowlist must mirror local-claude`,
      );
    }
  }
  // local-claude-deploy is asserted separately: DB-key auth only (no tokenFile),
  // 30-day key TTL, and exactly the two privileged servers — nothing the trio has.
  const deploy = policy.clients["local-claude-deploy"];
  assert.equal(deploy.tokenFile, undefined, "local-claude-deploy must have no tokenFile");
  assert.equal(deploy.keyTtlDays, 30);
  assert.deepEqual(Object.keys(deploy.servers).sort(), ["builder", "container-manager"]);
});

// Unit coverage for every argLimits kind the gateway enforces (Task 1), with a
// synthetic limit set so each rule is exercised independently of the manifest.
const unitLimits = {
  argLimits: {
    t: {
      _onlyArgs: ["name", "kind", "skip", "n", "map", "build_args"],
      name: { required: true, type: "string", pattern: "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$" },
      kind: { type: "string", in: ["web", "api"] },
      skip: { type: "string", notIn: ["blocked"] },
      n: { max: 5 },
      map: { mapKeyPattern: "^[A-Za-z0-9_-]+$", mapValuePattern: "^https://github\\.com/BuildMy-house/[A-Za-z0-9._-]+\\.git#[0-9a-f]{40}$" },
      build_args: { mapKeyPattern: "^[A-Z][A-Z0-9_]*$", mapValuePattern: "^[A-Za-z0-9._:/-]{0,128}$" },
    },
  },
};

test("violatesLimits: required, type, max, pattern, in, notIn, mapKey/ValuePattern, _onlyArgs", () => {
  const call = (args) => violatesLimits(unitLimits, "t", args);
  // required: absent (and null) is a violation; present passes.
  assert.match(call({ kind: "web", name: undefined }), /name is required/);
  assert.match(call({ kind: "web", name: null }), /name is required/);
  assert.equal(call({ kind: "web", name: "job-1" }), null);
  // type: numbers, arrays, objects rejected when present; strings pass.
  assert.match(call({ name: 42 }), /name must be a string/);
  assert.match(call({ name: ["hermes-gateway"] }), /name must be a string/);
  assert.match(call({ name: { a: 1 } }), /name must be a string/);
  // pattern: anchored $ must not accept a trailing newline, and a non-string
  // value with a pattern is a violation (no String() coercion).
  assert.match(call({ name: "job-1\n" }), /name does not match required pattern/);
  assert.match(call({ name: ["job-1"] }), /name must be a string/);
  assert.match(call({ name: 5 }), /name must be a string/);
  assert.match(call({ name: "" }), /name does not match required pattern/);
  // in / notIn: exact string match only.
  assert.match(call({ name: "job-1", kind: "db" }), /kind is not in the allowed list/);
  assert.match(call({ name: "job-1", kind: 1 }), /kind must be a string/);
  assert.match(call({ name: "job-1", kind: "web", skip: "blocked" }), /skip is in notIn list/);
  assert.equal(call({ name: "job-1", kind: "web", skip: "fine" }), null);
  // max: backward compatible, default fills absent values (unchanged behaviour).
  assert.match(call({ name: "job-1", n: 9 }), /n exceeds 5/);
  assert.equal(call({ name: "job-1", n: 5 }), null);
  assert.equal(call({ name: "job-1" }), null);
  // mapKeyPattern / mapValuePattern: non-object (or array) value is a violation;
  // every key and every (string) value must match.
  const goodRepo = "https://github.com/BuildMy-house/company-os.git#0123456789abcdef0123456789abcdef01234567";
  assert.equal(call({ name: "job-1", map: { shared: goodRepo } }), null);
  assert.equal(call({ name: "job-1", map: {} }), null);
  assert.match(call({ name: "job-1", map: "https://x" }), /map must be an object/);
  assert.match(call({ name: "job-1", map: [goodRepo] }), /map must be an object/);
  assert.match(call({ name: "job-1", map: { "bad key": goodRepo } }), /map has a key not matching mapKeyPattern/);
  assert.match(call({ name: "job-1", map: { shared: "https://github.com/evil/x.git#0123456789abcdef0123456789abcdef01234567" } }), /map has a value not matching mapValuePattern/);
  assert.match(call({ name: "job-1", map: { shared: 42 } }), /map has a value not matching mapValuePattern/);
  assert.match(call({ name: "job-1", map: { shared: `x\n` } }), /map has a value not matching mapValuePattern/);
  assert.equal(call({ name: "job-1", build_args: { FLAVOR: "opencode", PATH2: "a/b" } }), null);
  assert.match(call({ name: "job-1", build_args: { lowercase: "x" } }), /build_args has a key not matching mapKeyPattern/);
  assert.match(call({ name: "job-1", build_args: { FLAVOR: { nested: true } } }), /build_args has a value not matching mapValuePattern/);
  // _onlyArgs: extra arg key rejected, and the _onlyArgs entry itself is never
  // treated as a field by the field loop.
  assert.match(call({ name: "job-1", evil: "1" }), /argument evil is not permitted/);
  assert.match(call({ name: "job-1", n: 1, _onlyArgs: ["name", "n"] }), /argument _onlyArgs is not permitted/);
  // args must be a plain object when argLimits exist for the tool.
  assert.match(violatesLimits(unitLimits, "t", ["name"]), /arguments must be an object/);
  assert.match(violatesLimits(unitLimits, "t", "name"), /arguments must be an object/);
  assert.match(violatesLimits(unitLimits, "t", null), /arguments must be an object/);
  // No argLimits for the tool: no argument-shape enforcement at all (compat).
  assert.equal(violatesLimits(unitLimits, "unknown_tool", ["whatever"]), null);
  assert.equal(violatesLimits({}, "t", ["whatever"]), null);
});

test("manifest policy: local-claude-deploy allowlists are exactly 5 container tools + 1 builder tool, no other server", () => {
  const policy = configMapPolicy(manifest, "mcp-gateway-policy");
  const deploy = policy.clients["local-claude-deploy"];
  assert.deepEqual(Object.keys(deploy.servers).sort(), ["builder", "container-manager"]);
  assert.deepEqual(deploy.servers["container-manager"].tools, ["container_upgrade", "container_rollback", "container_status", "container_health", "container_logs"]);
  assert.deepEqual(deploy.servers.builder.tools, ["builder_build_and_push"]);
  assert.ok(!deploy.servers.hive, "no hive");
  assert.ok(!deploy.servers.kubernetes, "no kubernetes");
});

test("manifest policy: the three local-* clients cannot reach container-manager or builder", () => {
  const policy = configMapPolicy(manifest, "mcp-gateway-policy");
  for (const id of ["local-claude", "local-opencode", "local-codex"]) {
    assert.equal(policy.clients[id].servers["container-manager"], undefined, `${id} must not reach container-manager`);
    assert.equal(policy.clients[id].servers.builder, undefined, `${id} must not reach builder`);
  }
});

test("manifest policy: violatesLimits against the real container-manager argLimits", () => {
  const policy = configMapPolicy(manifest, "mcp-gateway-policy");
  const cm = policy.clients["local-claude-deploy"].servers["container-manager"];
  const hex = "a".repeat(64);
  const good = { deployment: "hive-coordinator", image: `localhost:30500/company-os-hive@sha256:${hex}` };
  assert.equal(violatesLimits(cm, "container_upgrade", good), null);
  assert.equal(violatesLimits(cm, "container_upgrade", { deployment: "mcp", image: `registry.company-ops.svc.cluster.local:5000/company-os@sha256:${hex}` }), null);
  assert.ok(violatesLimits(cm, "container_upgrade", { ...good, deployment: "hermes-gateway" }));
  assert.ok(violatesLimits(cm, "container_upgrade", { ...good, deployment: "mcp-gateway" }));
  assert.ok(violatesLimits(cm, "container_upgrade", { ...good, deployment: "hermes-gateway\n" }), "trailing newline must not slip the anchored pattern");
  assert.ok(violatesLimits(cm, "container_upgrade", { deployment: "hive-coordinator", image: "localhost:30500/company-os-hive:main" }), "unpinned tag");
  assert.ok(violatesLimits(cm, "container_upgrade", { deployment: "hive-coordinator", image: "localhost:30500/company-os-hive@sha256:zz" }), "short digest");
  assert.ok(violatesLimits(cm, "container_upgrade", { deployment: "hive-coordinator", image: `docker.io/company-os-hive@sha256:${hex}` }), "foreign registry");
  assert.ok(violatesLimits(cm, "container_upgrade", { deployment: "hive-coordinator", image: `evil.com/x/company-os-hive@sha256:${hex}` }), "registry outside the allowlist");
  assert.ok(violatesLimits(cm, "container_upgrade", { image: `localhost:30500/x@sha256:${hex}` }), "missing deployment");
  assert.ok(violatesLimits(cm, "container_upgrade", { ...good, deployment: "../x" }), "path traversal deployment");
  assert.ok(violatesLimits(cm, "container_upgrade", { ...good, deployment: ["hermes-gateway"] }), "array deployment");
  assert.ok(violatesLimits(cm, "container_upgrade", { ...good, deployment: 42 }), "numeric deployment");
  assert.ok(violatesLimits(cm, "container_upgrade", { ...good, extra: "x" }), "extra arg key");
  for (const tool of ["container_rollback", "container_health", "container_logs"]) {
    assert.ok(violatesLimits(cm, tool, { deployment: "hermes-gateway" }), `${tool} must deny hermes-gateway`);
    assert.ok(violatesLimits(cm, tool, { deployment: "mcp-gateway" }), `${tool} must deny mcp-gateway`);
    assert.equal(violatesLimits(cm, tool, { deployment: "hive-coordinator" }), null, `${tool} must allow hive-coordinator`);
  }
  assert.ok(violatesLimits(cm, "container_logs", { deployment: "hive-coordinator", tail_lines: 501 }), "tail_lines > 500");
  assert.equal(violatesLimits(cm, "container_logs", { deployment: "hive-coordinator", tail_lines: 500 }), null);
  assert.equal(violatesLimits(cm, "container_status", {}), null);
  assert.ok(violatesLimits(cm, "container_status", { deployment: "x" }), "_onlyArgs: [] rejects any argument");
});

test("manifest policy: violatesLimits against the real builder argLimits", () => {
  const policy = configMapPolicy(manifest, "mcp-gateway-policy");
  const builder = policy.clients["local-claude-deploy"].servers.builder;
  const sha = "0123456789abcdef0123456789abcdef01234567";
  const ref = `https://github.com/BuildMy-house/company-os.git#${sha}`;
  const valid = { context_ref: ref, dockerfile_path: "Dockerfile", image_repo: "company-os", image_tag: "abc123" };
  const good = (args) => violatesLimits(builder, "builder_build_and_push", args);
  assert.equal(good(valid), null);
  assert.equal(good({ ...valid, contexts: { shared: `https://github.com/BuildMy-house/workspace.git#${sha}` } }), null);
  assert.equal(good({ ...valid, build_args: { AGENT_FLAVOR: "opencode" }, timeout_seconds: 3600 }), null);
  assert.ok(good({ ...valid, context_ref: "https://github.com/BuildMy-house/company-os.git#main" }), "branch ref");
  assert.ok(good({ ...valid, context_ref: `https://github.com/other-org/company-os.git#${sha}` }), "non-BuildMy-house org");
  assert.ok(good({ ...valid, context_ref: `https://github.com/BuildMy-house/company-os.git#0123456` }), "short sha");
  assert.ok(good({ ...valid, context_ref: `https://user:pw@github.com/BuildMy-house/company-os.git#${sha}` }), "embedded credentials");
  assert.ok(good({ ...valid, context_ref: `http://github.com/BuildMy-house/company-os.git#${sha}` }), "not https");
  assert.ok(good({ ...valid, image_repo: "company-os-mcp-gateway" }), "gateway repo must not build itself");
  assert.ok(good({ ...valid, image_repo: "some-other-repo" }), "repo not in allowlist");
  assert.ok(good({ ...valid, dockerfile_path: "../Dockerfile" }), "dockerfile_path with ..");
  assert.ok(good({ ...valid, image_tag: "bad tag!" }), "invalid image_tag");
  assert.ok(good({ context_ref: ref, dockerfile_path: "Dockerfile", image_tag: "abc" }), "missing required image_repo");
  assert.ok(good({ ...valid, contexts: { shared: `https://github.com/evil/repo.git#${sha}` } }), "contexts value from another host");
  assert.ok(good({ ...valid, contexts: { "bad key": ref } }), "contexts key not matching mapKeyPattern");
  assert.ok(good({ ...valid, build_args: { lowercase: "x" } }), "build_args key style");
  assert.ok(good({ ...valid, evil: "x" }), "extra arg key");
  assert.ok(good({ ...valid, timeout_seconds: 4000 }), "timeout_seconds over 3600");
});

// End-to-end: a real gateway enforcing the REAL local-claude-deploy
// container-manager tools+argLimits against a fake child that records every
// tools/call it receives, so denials are proven to never reach the child.
const cmChild = join(dir, "cm-child.mjs");
const cmCallLog = join(dir, "cm-calls.log");
writeFileSync(cmChild, `
import readline from "node:readline";
import { appendFileSync } from "node:fs";
const tools = ["container_upgrade","container_rollback","container_status","container_health","container_logs","container_restart","container_test"].map((name) => ({ name, inputSchema: { type: "object" } }));
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.id === undefined) return;
  const send = (result) => console.log(JSON.stringify({ jsonrpc: "2.0", id: m.id, result }));
  if (m.method === "initialize") return send({ protocolVersion: "2025-03-26", capabilities: {} });
  if (m.method === "tools/list") return send({ tools });
  if (m.method === "tools/call") {
    appendFileSync(process.env.CALL_LOG, JSON.stringify({ name: m.params.name, args: m.params.arguments }) + "\\n");
    return send({ content: [{ type: "text", text: "ok" }] });
  }
  send({});
});
`);
writeFileSync(join(dir, "deploy.tok"), "token-deploy\n");

test("e2e: deploy identity allowlist + argLimits deny before the fake child sees a call", async () => {
  const policy = configMapPolicy(manifest, "mcp-gateway-policy");
  const real = policy.clients["local-claude-deploy"].servers["container-manager"];
  const gw2 = createGateway({
    policy: {
      servers: { "container-manager": { command: ["node", cmChild], env: { CALL_LOG: cmCallLog } } },
      clients: { deploy: { tokenFile: join(dir, "deploy.tok"), servers: { "container-manager": { tools: real.tools, argLimits: real.argLimits } } } },
    },
    env: {},
  });
  try {
    await new Promise((r) => gw2.server.listen(0, r));
    const base2 = `http://127.0.0.1:${gw2.server.address().port}`;
    const call = async (name, args) => {
      const res = await fetch(`${base2}/container-manager`, {
        method: "POST",
        headers: { authorization: "Bearer token-deploy", "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }),
      });
      return res.json();
    };
    for (const name of ["container_restart", "container_test", "forbidden"]) {
      const denied = await call(name, {});
      assert.equal(denied.error?.code, -32001, `${name} must be denied as not in allowlist`);
    }
    const bad = await call("container_upgrade", { deployment: "hermes-gateway", image: `localhost:30500/x@sha256:${"a".repeat(64)}` });
    assert.equal(bad.error?.code, -32001, "upgrading hermes-gateway must be denied by argLimits");
    const ok = await call("container_status", {});
    assert.ok(ok.result, "an allowed call must reach the fake child");
    const recorded = readFileSync(cmCallLog, "utf8").trim().split("\n").map((line) => JSON.parse(line).name);
    assert.deepEqual(recorded, ["container_status"], "denied calls must never reach the child");
  } finally {
    gw2.close();
  }
});

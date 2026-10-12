#!/usr/bin/env node

// Supervised Streamable-HTTP MCP gateway (docs/MCP-GATEWAY.md,
// Steward documents/plans/mcp-gateway + mcp-gateway-decisions).
//
// Hosts the stdio MCP servers in this repo as children and exposes each at
// POST /<server> for local clients. Stateless: no Mcp-Session-Id is ever issued, so a
// gateway restart leaves nothing for a client to lose; the next call just works.
//   - auth: one bearer token per client identity, read from a file on every request
//   - policy: default-deny per-client tool allowlist, enforced on tools/list AND tools/call
//   - supervision: a crashed child is respawned with backoff; in-flight calls get a JSON-RPC error
//   - audit: one event per tools/call (argument hash only, never the arguments)
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createHash, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const PROTOCOL_VERSION = "2025-03-26";
const KEEPALIVE_MS = 15_000;
const MAX_BODY_BYTES = 1_000_000;
const BACKOFF_START_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const HEALTHY_RESET_MS = 60_000;

const REDACTIONS = [
  /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}/g,
  /\b(?:ghp|ghs|gho|ghu|github_pat)_[A-Za-z0-9_]{20,}/g,
  /\bsk-[A-Za-z0-9_-]{20,}/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
];

export function redact(text) {
  return REDACTIONS.reduce((out, pattern) => out.replace(pattern, "[REDACTED]"), text);
}

export function loadPolicy(file) {
  const policy = JSON.parse(readFileSync(file, "utf8"));
  if (!policy.servers || !policy.clients) throw new Error("policy needs servers and clients");
  for (const [name, server] of Object.entries(policy.servers)) {
    if (!Array.isArray(server.command) || !server.command.length) throw new Error(`server ${name}: command must be a non-empty array`);
  }
  for (const [id, client] of Object.entries(policy.clients)) {
    if (!client.tokenFile) throw new Error(`client ${id}: tokenFile required`);
    for (const name of Object.keys(client.servers || {})) {
      if (!policy.servers[name]) throw new Error(`client ${id}: unknown server ${name}`);
      if (!Array.isArray(client.servers[name].tools)) throw new Error(`client ${id}/${name}: tools must be an explicit array (default-deny)`);
    }
  }
  return policy;
}

function tokenMatches(presented, expected) {
  const a = createHash("sha256").update(presented).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

// Resolve the calling identity from the Authorization header. Token files are re-read
// per request so rotation (Infisical sync updating the mounted Secret) needs no restart.
export function authenticate(policy, header) {
  const presented = /^Bearer\s+(\S+)$/i.exec(header || "")?.[1];
  if (!presented) return null;
  let found = null;
  for (const [id, client] of Object.entries(policy.clients)) {
    let expected;
    try { expected = readFileSync(client.tokenFile, "utf8").trim(); } catch { continue; }
    if (expected && tokenMatches(presented, expected) && !found) found = id;
  }
  return found;
}

// A supervised stdio MCP child: respawn with backoff, id remapping, handshake on every spawn.
class Child {
  constructor({ key, command, env, audit }) {
    this.key = key;
    this.command = command;
    this.env = env;
    this.audit = audit;
    this.pending = new Map();
    this.nextId = 1;
    this.restarts = 0;
    this.backoff = BACKOFF_START_MS;
    this.ready = null;
    this.proc = null;
    this.stopped = false;
    this.start();
  }

  start() {
    const startedAt = Date.now();
    const proc = spawn(this.command[0], this.command.slice(1), { env: { ...process.env, ...this.env }, stdio: ["pipe", "pipe", "inherit"] });
    this.proc = proc;
    let buffer = "";
    proc.stdout.on("data", (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        this.onLine(line);
      }
    });
    proc.stdin.on("error", () => {});
    proc.on("error", (error) => console.error(`[mcp-gateway] ${this.key} spawn error: ${error.message}`));
    proc.on("exit", (code, signal) => this.onExit(proc, startedAt, code, signal));
    this.ready = this.handshake().catch((error) => { throw error; });
    this.ready.catch(() => {});
  }

  async handshake() {
    await this.rawRequest("initialize", { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: { name: "mcp-gateway", version: "1" } }, 20_000);
    this.proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })}\n`);
  }

  onLine(line) {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (message.id === undefined || !this.pending.has(message.id)) return; // child notifications are not forwarded
    const { resolve, timer } = this.pending.get(message.id);
    clearTimeout(timer);
    this.pending.delete(message.id);
    resolve(message);
  }

  onExit(proc, startedAt, code, signal) {
    if (proc !== this.proc) return;
    for (const { reject, timer } of this.pending.values()) { clearTimeout(timer); reject(new Error("child exited")); }
    this.pending.clear();
    if (this.stopped) return;
    if (Date.now() - startedAt > HEALTHY_RESET_MS) this.backoff = BACKOFF_START_MS;
    const delay = this.backoff;
    this.backoff = Math.min(BACKOFF_MAX_MS, this.backoff * 2);
    this.restarts += 1;
    this.proc = null;
    this.ready = Promise.reject(new Error("child restarting"));
    this.ready.catch(() => {});
    console.error(`[mcp-gateway] ${this.key} exited (code=${code} signal=${signal}); respawn in ${delay}ms`);
    this.audit({ event: "child_exit", child: this.key, code, signal, restarts: this.restarts });
    setTimeout(() => { if (!this.stopped) { this.start(); this.audit({ event: "child_respawn", child: this.key, restarts: this.restarts }); } }, delay).unref();
  }

  rawRequest(method, params, timeoutMs) {
    return new Promise((resolve, reject) => {
      if (!this.proc?.stdin.writable) return reject(new Error("child not running"));
      const id = this.nextId++;
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`child request timed out after ${timeoutMs}ms`)); }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.proc.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  async request(method, params, timeoutMs = 60_000) {
    await this.ready;
    return this.rawRequest(method, params, timeoutMs);
  }

  stop() {
    this.stopped = true;
    this.proc?.kill();
  }
}

export function createGateway({ policy, env = process.env, fetchImpl = fetch } = {}) {
  const children = new Map();
  const inflightLong = new Map();
  const allowedOrigins = new Set((env.MCP_GATEWAY_ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean));

  function audit(event) {
    const record = { _time: new Date().toISOString(), service: env.AXIOM_SERVICE_NAME || "mcp-gateway", environment: env.DEPLOYMENT_ENVIRONMENT || "unknown", ...event };
    console.log(JSON.stringify(record));
    if (!env.AXIOM_TOKEN) return;
    const dataset = env.AXIOM_DATASET || "bmh-company";
    // Fire-and-forget: an Axiom outage must never delay or fail a tool call.
    void fetchImpl(`https://eu-central-1.aws.edge.axiom.co/v1/ingest/${dataset}`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.AXIOM_TOKEN}`, "content-type": "application/json" },
      body: JSON.stringify([record]),
      signal: AbortSignal.timeout(5_000),
    }).catch(() => {});
  }

  // One child per server, or per (client, server) when the policy sets per-identity env (hive).
  function childFor(clientId, serverName) {
    const server = policy.servers[serverName];
    const clientServer = policy.clients[clientId].servers[serverName];
    const perIdentity = Boolean(clientServer.agentId);
    const key = perIdentity ? `${serverName}:${clientId}` : serverName;
    if (!children.has(key)) {
      const childEnv = { ...(server.env || {}), ...(perIdentity ? { HIVE_AGENT_ID: clientServer.agentId } : {}) };
      children.set(key, new Child({ key, command: server.command, env: childEnv, audit }));
    }
    return children.get(key);
  }

  const rpcError = (id, code, message) => ({ jsonrpc: "2.0", id: id ?? null, error: { code, message } });
  const argsHash = (args) => createHash("sha256").update(JSON.stringify(args ?? {})).digest("hex");

  function violatesLimits(clientServer, tool, args) {
    for (const [field, limit] of Object.entries(clientServer.argLimits?.[tool] || {})) {
      const value = args?.[field];
      if (limit.max !== undefined && Number(value ?? limit.default ?? 0) > limit.max) return `${field} exceeds ${limit.max}`;
      if (limit.pattern && value !== undefined && !new RegExp(limit.pattern).test(String(value))) return `${field} does not match required pattern`;
    }
    return null;
  }

  async function handleRpc(clientId, serverName, message) {
    const clientServer = policy.clients[clientId].servers[serverName];
    const server = policy.servers[serverName];
    const { id, method, params } = message;
    if (method === "initialize") {
      return { jsonrpc: "2.0", id, result: { protocolVersion: params?.protocolVersion || PROTOCOL_VERSION, capabilities: { tools: {} }, serverInfo: { name: `mcp-gateway/${serverName}`, version: "1" } } };
    }
    if (method === "ping") return { jsonrpc: "2.0", id, result: {} };
    if (method === "tools/list") {
      const reply = await childFor(clientId, serverName).request("tools/list", {}, 20_000);
      if (reply.error) return { ...reply, id };
      const tools = (reply.result?.tools || []).filter((tool) => clientServer.tools.includes(tool.name));
      audit({ event: "tools_list", client_id: clientId, server: serverName, count: tools.length });
      return { jsonrpc: "2.0", id, result: { tools } };
    }
    if (method === "tools/call") {
      const tool = params?.name;
      const args = params?.arguments || {};
      const started = Date.now();
      const base = { client_id: clientId, server: serverName, tool, args_sha256: argsHash(args), request_bytes: JSON.stringify(params ?? {}).length };
      const deny = (reason) => { audit({ event: "tools_call", ...base, outcome: "denied", error_class: reason, duration_ms: Date.now() - started }); return rpcError(id, -32001, `tool not permitted: ${tool}`); };
      if (typeof tool !== "string" || !clientServer.tools.includes(tool)) return deny("not_in_allowlist");
      const limited = violatesLimits(clientServer, tool, args);
      if (limited) return deny(`arg_limit: ${limited}`);
      const isLong = (server.longPollTools || []).includes(tool);
      const longKey = `${clientId}:${serverName}`;
      if (isLong) {
        if ((inflightLong.get(longKey) || 0) >= (policy.maxConcurrentLongPolls || 4)) return deny("too_many_long_polls");
        inflightLong.set(longKey, (inflightLong.get(longKey) || 0) + 1);
      }
      try {
        const child = childFor(clientId, serverName);
        const reply = await child.request("tools/call", { name: tool, arguments: args }, (server.callTimeoutSeconds || 960) * 1000);
        let out = { ...reply, id };
        if (out.result?.content && (server.redactTools || []).includes(tool)) {
          out = { ...out, result: { ...out.result, content: out.result.content.map((part) => (typeof part.text === "string" ? { ...part, text: redact(part.text) } : part)) } };
        }
        audit({ event: "tools_call", ...base, outcome: reply.error || reply.result?.isError ? "error" : "ok", duration_ms: Date.now() - started, response_bytes: JSON.stringify(out).length, child_restarts: child.restarts });
        return out;
      } catch (error) {
        audit({ event: "tools_call", ...base, outcome: "error", error_class: "child_unavailable", duration_ms: Date.now() - started });
        return rpcError(id, -32002, `upstream unavailable, retry: ${error.message}`);
      } finally {
        if (isLong) inflightLong.set(longKey, inflightLong.get(longKey) - 1);
      }
    }
    return rpcError(id, -32601, `method not found: ${method}`);
  }

  async function readBody(request) {
    let size = 0;
    const chunks = [];
    for await (const chunk of request) {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) throw Object.assign(new Error("body too large"), { status: 413 });
      chunks.push(chunk);
    }
    return Buffer.concat(chunks).toString("utf8");
  }

  function reply(response, status, body, headers = {}) {
    response.writeHead(status, { "content-type": "application/json", ...headers });
    response.end(JSON.stringify(body));
  }

  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://gateway");
      if (url.pathname === "/healthz") return reply(response, 200, { ok: true });
      if (url.pathname === "/readyz") {
        const states = Object.fromEntries([...children].map(([key, child]) => [key, { running: Boolean(child.proc), restarts: child.restarts }]));
        return reply(response, 200, { ok: true, children: states });
      }
      const serverName = url.pathname.slice(1);
      if (!policy.servers[serverName]) return reply(response, 404, { error: "not found" });
      const origin = request.headers.origin;
      if (origin && !allowedOrigins.has(origin)) { audit({ event: "origin_denied", origin }); return reply(response, 403, { error: "origin not allowed" }); }
      const clientId = authenticate(policy, request.headers.authorization);
      if (!clientId) {
        const presented = /^Bearer\s+(\S+)$/i.exec(request.headers.authorization || "")?.[1] || "";
        audit({ event: "auth_failure", server: serverName, token_fingerprint: presented ? createHash("sha256").update(presented).digest("hex").slice(0, 8) : null });
        return reply(response, 401, { error: "unauthorized" }, { "www-authenticate": "Bearer" });
      }
      if (!policy.clients[clientId].servers?.[serverName]) { audit({ event: "policy_denied", client_id: clientId, server: serverName }); return reply(response, 403, { error: "server not permitted for this identity" }); }
      if (request.method !== "POST") return reply(response, 405, { error: "POST only; no standalone stream" }, { allow: "POST" });

      let message;
      try { message = JSON.parse(await readBody(request)); } catch (error) { return reply(response, error.status || 400, rpcError(null, -32700, "parse error")); }
      if (Array.isArray(message) || typeof message !== "object" || message === null) return reply(response, 400, rpcError(null, -32600, "single JSON-RPC message required"));
      if (message.id === undefined) { response.writeHead(202).end(); return; } // notification

      const tool = message.params?.name;
      const streaming = message.method === "tools/call" && (policy.servers[serverName].longPollTools || []).includes(tool);
      if (!streaming) return reply(response, 200, await handleRpc(clientId, serverName, message));

      response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
      const keepalive = setInterval(() => response.write(": keepalive\n\n"), KEEPALIVE_MS);
      response.on("close", () => clearInterval(keepalive));
      const result = await handleRpc(clientId, serverName, message);
      clearInterval(keepalive);
      response.end(`event: message\ndata: ${JSON.stringify(result)}\n\n`);
    } catch (error) {
      console.error(`[mcp-gateway] request failed: ${error.message}`);
      if (!response.headersSent) reply(response, 500, { error: "internal error" });
      else response.end();
    }
  });

  return {
    server,
    children,
    close() {
      for (const child of children.values()) child.stop();
      server.closeAllConnections?.();
      server.close();
    },
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const policy = loadPolicy(process.env.MCP_GATEWAY_POLICY || "/etc/mcp-gateway/policy.json");
  const gateway = createGateway({ policy });
  const port = Number(process.env.PORT || 8080);
  gateway.server.listen(port, () => console.error(`[mcp-gateway] listening on :${port}`));
  for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => { gateway.close(); setTimeout(() => process.exit(0), 500).unref(); });
}

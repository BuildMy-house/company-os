#!/usr/bin/env node

// Supervised Streamable-HTTP MCP gateway (docs/MCP-GATEWAY.md,
// Steward documents/plans/mcp-gateway + mcp-gateway-decisions).
//
// Hosts the stdio MCP servers in this repo as children and exposes each at
// POST /<server> for local clients. Stateless: no Mcp-Session-Id is ever issued, so a
// gateway restart leaves nothing for a client to lose; the next call just works.
//   - auth: issued API keys (gwk_..., SHA-256 hashed in Postgres, scripts/mcp-gateway-keys.js)
//     bound to exactly one policy identity; until an identity has an issued key its legacy
//     token file still works (legacyFileTokenAuth, to be removed once all are migrated).
//     The gateway-admin identity (bootstrap token file) alone manages keys at /gateway-admin.
//   - policy: default-deny per-client tool allowlist, enforced on tools/list AND tools/call
//   - supervision: a crashed child is respawned with backoff; in-flight calls get a JSON-RPC error
//   - audit: one event per tools/call (argument hash only, never the arguments)
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { createHash, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createKeyService, createPgKeyStore, KEY_PREFIX } from "./mcp-gateway-keys.js";

const PROTOCOL_VERSION = "2025-03-26";
const KEEPALIVE_MS = 15_000;
const MAX_BODY_BYTES = 1_000_000;
const BACKOFF_START_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const HEALTHY_RESET_MS = 60_000;
export const ADMIN_SERVER = "gateway-admin";
const DEFAULT_KEY_TTL_DAYS = 90;
const KEY_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const REDACTIONS = [
  /\bBearer\s+[A-Za-z0-9._~+/=-]{16,}/g,
  /\b(?:ghp|ghs|gho|ghu|github_pat)_[A-Za-z0-9_]{20,}/g,
  /\bsk-[A-Za-z0-9_-]{20,}/g,
  /\bAKIA[0-9A-Z]{16}\b/g,
  /\bgwk_[A-Za-z0-9_-]{20,}/g,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
];

export function redact(text) {
  return REDACTIONS.reduce((out, pattern) => out.replace(pattern, "[REDACTED]"), text);
}

// Default-deny argument limits (argLimits.<tool>.<field>), evaluated before a tool
// call reaches its child. Violation strings are short and never echo the offending
// value — they land in audit logs (error_class) and go back to the caller.
//   _onlyArgs: tool-level key (not a field); any argument key outside the list is a violation
//   required: field must be present (not undefined/null)
//   type: "string": present value must be a JS string (numbers/arrays/objects rejected)
//   max: numeric ceiling (backward compatible; limit.default fills in when absent)
//   pattern: compiled as written (policies use explicit ^...$ anchors). Matched against
//     the raw string value — a non-string value is a violation, and a match that does
//     not consume the whole input is too, so "x\n" cannot slip an anchored pattern via
//     the pre-final-newline meaning of `$`. Absent values skip the check (unchanged).
//   in / notIn: exact string allowlist / blocklist
//   mapKeyPattern / mapValuePattern: for object-valued fields, every key / every string
//     value must match; a non-object (or array) value is a violation.
export function violatesLimits(clientServer, tool, args) {
  const limits = clientServer.argLimits?.[tool] || {};
  const entries = Object.entries(limits);
  if (!entries.length) return null;
  if (args === null || typeof args !== "object" || Array.isArray(args)) return "arguments must be an object";
  for (const [field, limit] of entries) {
    if (field === "_onlyArgs") continue;
    const value = args[field];
    const present = value !== undefined && value !== null;
    if (limit.required && !present) return `${field} is required`;
    if (limit.type === "string" && present && typeof value !== "string") return `${field} must be a string`;
    if (limit.max !== undefined && Number(value ?? limit.default ?? 0) > limit.max) return `${field} exceeds ${limit.max}`;
    if (limit.pattern && present) {
      const match = typeof value === "string" ? new RegExp(limit.pattern).exec(value) : null;
      if (!match || match[0].length !== value.length) return `${field} does not match required pattern`;
    }
    if (limit.in && present && !limit.in.includes(value)) return `${field} is not in the allowed list`;
    if (limit.notIn && present && limit.notIn.includes(value)) return `${field} is in notIn list`;
    if ((limit.mapKeyPattern || limit.mapValuePattern) && present) {
      if (typeof value !== "object" || Array.isArray(value)) return `${field} must be an object`;
      for (const [key, entry] of Object.entries(value)) {
        if (limit.mapKeyPattern && !new RegExp(limit.mapKeyPattern).test(key)) return `${field} has a key not matching mapKeyPattern`;
        if (limit.mapValuePattern) {
          const match = typeof entry === "string" ? new RegExp(limit.mapValuePattern).exec(entry) : null;
          if (!match || match[0].length !== entry.length) return `${field} has a value not matching mapValuePattern`;
        }
      }
    }
  }
  const only = limits._onlyArgs;
  if (Array.isArray(only)) {
    for (const key of Object.keys(args)) {
      if (!only.includes(key)) return `argument ${key} is not permitted`;
    }
  }
  return null;
}

export function loadPolicy(file) {
  const policy = JSON.parse(readFileSync(file, "utf8"));
  if (!policy.servers || !policy.clients) throw new Error("policy needs servers and clients");
  for (const [name, server] of Object.entries(policy.servers)) {
    if (!Array.isArray(server.command) || !server.command.length) throw new Error(`server ${name}: command must be a non-empty array`);
  }
  if (policy.servers[ADMIN_SERVER]) throw new Error(`server name ${ADMIN_SERVER} is reserved for the in-process admin tools`);
  for (const [id, client] of Object.entries(policy.clients)) {
    if (client.admin) {
      // The admin identity is the bootstrap root of trust: file token only, no child servers, never key-issuable.
      if (!client.tokenFile) throw new Error(`client ${id}: admin identity requires tokenFile`);
      if (Object.keys(client.servers || {}).length) throw new Error(`client ${id}: admin identity cannot be granted child servers`);
    } else if (client.tokenFile !== undefined && typeof client.tokenFile !== "string") {
      throw new Error(`client ${id}: tokenFile must be a string (omit it for identities that use issued keys only)`);
    }
    if (client.keyTtlDays !== undefined && !(Number.isInteger(client.keyTtlDays) && client.keyTtlDays > 0 && client.keyTtlDays <= 365)) {
      throw new Error(`client ${id}: keyTtlDays must be an integer between 1 and 365`);
    }
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

// Match a presented token against the per-identity token files (re-read per request so an
// Infisical sync update needs no restart). Identities without a tokenFile (issued-keys-only)
// are skipped. Returns the identity or null.
export function matchFileToken(policy, presented) {
  let found = null;
  for (const [id, client] of Object.entries(policy.clients)) {
    if (!client.tokenFile) continue;
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

export function createGateway({ policy, env = process.env, fetchImpl = fetch, keyStore = null, now = () => new Date() } = {}) {
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

  // Issued-key service. Keys are bound to exactly one non-admin policy identity and are
  // evaluated against that identity's CURRENT policy on every request, so a key can never
  // exceed (or outlive) the allowlist/argLimits in the policy file.
  const nonAdminIdentity = (id) => Boolean(policy.clients[id]) && !policy.clients[id].admin;
  const keyService = keyStore
    ? createKeyService({ store: keyStore, identities: nonAdminIdentity, ttlDaysFor: (id) => policy.clients[id]?.keyTtlDays ?? DEFAULT_KEY_TTL_DAYS, now, audit })
    : null;
  const fingerprintOf = (token) => (token ? createHash("sha256").update(token).digest("hex").slice(0, 8) : null);

  // Transitional single path for pre-key identities: a file token is honoured only until the
  // identity has any issued key (then it is retired for good, even if every key is later
  // revoked). Remove this function and the tokenFile fields in the follow-up step documented
  // in docs/MCP-GATEWAY.md once every identity has a key.
  async function legacyFileTokenAuth(identity) {
    if (!keyService) return { clientId: identity };
    try {
      if (await keyService.hasIssuedKeys(identity)) return { denied: "file_token_retired", status: 401 };
    } catch {
      return { denied: "auth_store_unavailable", status: 503 };
    }
    return { clientId: identity };
  }

  async function authenticateRequest(header) {
    const presented = /^Bearer\s+(\S+)$/i.exec(header || "")?.[1];
    if (!presented) return { denied: "missing", status: 401, fingerprint: null };
    const fp = fingerprintOf(presented);
    if (presented.startsWith(KEY_PREFIX)) {
      if (!keyService) return { denied: "key_store_not_configured", status: 401, fingerprint: fp };
      let result;
      try { result = await keyService.verifyDetailed(presented); } catch { return { denied: "auth_store_unavailable", status: 503, fingerprint: fp }; }
      if (!result.ok) return { denied: result.reason, status: 401, fingerprint: fp };
      if (!nonAdminIdentity(result.identity)) return { denied: "identity_not_in_policy", status: 401, fingerprint: fp };
      return { clientId: result.identity, keyId: result.keyId, fingerprint: fp };
    }
    const identity = matchFileToken(policy, presented);
    if (!identity) return { denied: "unknown", status: 401, fingerprint: fp };
    if (policy.clients[identity].admin) return { clientId: identity, fingerprint: fp };
    return { ...(await legacyFileTokenAuth(identity)), fingerprint: fp };
  }

  const ADMIN_TOOLS = [
    { name: "issue_key", description: "Issue a new API key for one policy identity. The plaintext is returned once and never stored.", inputSchema: { type: "object", required: ["identity", "label"], properties: { identity: { type: "string" }, label: { type: "string", maxLength: 80 }, ttl_days: { type: "integer", minimum: 1, maximum: 365 } }, additionalProperties: false } },
    { name: "rotate_key", description: "Issue a replacement key for the same identity and label; the old key keeps working for overlap_seconds.", inputSchema: { type: "object", required: ["key_id"], properties: { key_id: { type: "string" }, overlap_seconds: { type: "integer", minimum: 0, maximum: 86400, default: 3600 } }, additionalProperties: false } },
    { name: "revoke_key", description: "Revoke one key immediately.", inputSchema: { type: "object", required: ["key_id"], properties: { key_id: { type: "string" } }, additionalProperties: false } },
    { name: "revoke_all_for_identity", description: "Revoke every key of an identity; the identity cannot authenticate again until a new key is issued.", inputSchema: { type: "object", required: ["identity"], properties: { identity: { type: "string" } }, additionalProperties: false } },
    { name: "list_keys", description: "List keys (id, identity, label, 8-char fingerprint, timestamps, status). Never returns hashes or plaintext.", inputSchema: { type: "object", properties: { identity: { type: "string" } }, additionalProperties: false } },
  ];
  const isStr = (v) => typeof v === "string" && v.length > 0;
  const isIntIn = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;

  async function runAdminTool(adminId, tool, args) {
    if (!keyService) throw new Error("key store not configured (MCP_GATEWAY_DATABASE_URL is unset)");
    const only = (allowed) => { for (const k of Object.keys(args)) if (!allowed.includes(k)) throw new Error(`argument ${k} is not permitted`); };
    switch (tool) {
      case "issue_key":
        only(["identity", "label", "ttl_days"]);
        if (!isStr(args.identity) || !isStr(args.label)) throw new Error("identity and label are required strings");
        if (args.ttl_days !== undefined && !isIntIn(args.ttl_days, 1, 365)) throw new Error("ttl_days must be an integer 1-365");
        return keyService.issue({ identity: args.identity, label: args.label, createdBy: adminId, ttlDays: args.ttl_days });
      case "rotate_key":
        only(["key_id", "overlap_seconds"]);
        if (!isStr(args.key_id) || !KEY_ID_RE.test(args.key_id)) throw new Error("key_id must be a key UUID from list_keys");
        if (args.overlap_seconds !== undefined && !isIntIn(args.overlap_seconds, 0, 86_400)) throw new Error("overlap_seconds must be an integer 0-86400");
        return keyService.rotate({ keyId: args.key_id, overlapSeconds: args.overlap_seconds, createdBy: adminId });
      case "revoke_key":
        only(["key_id"]);
        if (!isStr(args.key_id) || !KEY_ID_RE.test(args.key_id)) throw new Error("key_id must be a key UUID from list_keys");
        await keyService.revoke({ keyId: args.key_id, by: adminId });
        return { revoked: args.key_id };
      case "revoke_all_for_identity":
        only(["identity"]);
        if (!isStr(args.identity) || !nonAdminIdentity(args.identity)) throw new Error("identity must be a non-admin policy identity");
        return { identity: args.identity, revoked: await keyService.revokeAllForIdentity({ identity: args.identity, by: adminId }) };
      case "list_keys":
        only(["identity"]);
        if (args.identity !== undefined && !isStr(args.identity)) throw new Error("identity must be a string");
        return { keys: await keyService.list({ identity: args.identity }) };
      default:
        throw new Error("unknown admin tool");
    }
  }

  async function handleAdminRpc(adminId, message) {
    const { id, method, params } = message;
    if (method === "initialize") return { jsonrpc: "2.0", id, result: { protocolVersion: params?.protocolVersion || PROTOCOL_VERSION, capabilities: { tools: {} }, serverInfo: { name: `mcp-gateway/${ADMIN_SERVER}`, version: "1" } } };
    if (method === "ping") return { jsonrpc: "2.0", id, result: {} };
    if (method === "tools/list") return { jsonrpc: "2.0", id, result: { tools: ADMIN_TOOLS } };
    if (method === "tools/call") {
      const tool = params?.name;
      const args = params?.arguments || {};
      const started = Date.now();
      const base = { client_id: adminId, server: ADMIN_SERVER, tool, args_sha256: argsHash(args) };
      if (!ADMIN_TOOLS.some((t) => t.name === tool) || args === null || typeof args !== "object" || Array.isArray(args)) {
        audit({ event: "tools_call", ...base, outcome: "denied", error_class: "not_in_allowlist", duration_ms: Date.now() - started });
        return rpcError(id, -32001, `tool not permitted: ${tool}`);
      }
      try {
        const result = await runAdminTool(adminId, tool, args);
        const out = { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] } };
        audit({ event: "tools_call", ...base, outcome: "ok", duration_ms: Date.now() - started, response_bytes: JSON.stringify(out).length });
        return out;
      } catch (error) {
        audit({ event: "tools_call", ...base, outcome: "error", error_class: "admin_tool_error", duration_ms: Date.now() - started });
        return rpcError(id, -32000, error.message);
      }
    }
    return rpcError(id, -32601, `method not found: ${method}`);
  }

  const rpcError = (id, code, message) => ({ jsonrpc: "2.0", id: id ?? null, error: { code, message } });
  const argsHash = (args) => createHash("sha256").update(JSON.stringify(args ?? {})).digest("hex");

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
      const isAdminPath = serverName === ADMIN_SERVER && Object.values(policy.clients).some((c) => c.admin);
      if (!isAdminPath && !policy.servers[serverName]) return reply(response, 404, { error: "not found" });
      const origin = request.headers.origin;
      if (origin && !allowedOrigins.has(origin)) { audit({ event: "origin_denied", origin }); return reply(response, 403, { error: "origin not allowed" }); }
      const auth = await authenticateRequest(request.headers.authorization);
      if (!auth.clientId) {
        audit({ event: auth.status === 503 ? "auth_store_unavailable" : "auth_failure", server: serverName, reason: auth.denied, token_fingerprint: auth.fingerprint });
        if (auth.status === 503) return reply(response, 503, { error: "auth store unavailable" });
        return reply(response, 401, { error: "unauthorized" }, { "www-authenticate": "Bearer" });
      }
      const clientId = auth.clientId;
      if (isAdminPath ? !policy.clients[clientId].admin : !policy.clients[clientId].servers?.[serverName]) { audit({ event: "policy_denied", client_id: clientId, server: serverName }); return reply(response, 403, { error: "server not permitted for this identity" }); }
      if (request.method !== "POST") return reply(response, 405, { error: "POST only; no standalone stream" }, { allow: "POST" });

      let message;
      try { message = JSON.parse(await readBody(request)); } catch (error) { return reply(response, error.status || 400, rpcError(null, -32700, "parse error")); }
      if (Array.isArray(message) || typeof message !== "object" || message === null) return reply(response, 400, rpcError(null, -32600, "single JSON-RPC message required"));
      if (message.id === undefined) { response.writeHead(202).end(); return; } // notification

      const tool = message.params?.name;
      if (isAdminPath) return reply(response, 200, await handleAdminRpc(clientId, message));
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
  let pool = null;
  let keyStore = null;
  if (process.env.MCP_GATEWAY_DATABASE_URL) {
    const { default: pg } = await import("pg");
    pool = new pg.Pool({ connectionString: process.env.MCP_GATEWAY_DATABASE_URL, max: 4, connectionTimeoutMillis: 5_000 });
    pool.on("error", (error) => console.error(`[mcp-gateway] key store pool error: ${error.message}`));
    keyStore = createPgKeyStore(pool);
  } else {
    console.error("[mcp-gateway] MCP_GATEWAY_DATABASE_URL unset: file-token-only mode, key management disabled");
  }
  const gateway = createGateway({ policy, keyStore });
  const port = Number(process.env.PORT || 8080);
  gateway.server.listen(port, () => console.error(`[mcp-gateway] listening on :${port}`));
  for (const signal of ["SIGTERM", "SIGINT"]) process.on(signal, () => { gateway.close(); pool?.end().catch(() => {}); setTimeout(() => process.exit(0), 500).unref(); });
}

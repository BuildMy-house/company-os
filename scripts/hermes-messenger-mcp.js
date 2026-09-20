#!/usr/bin/env node

// Thin bridge from Claude/agent-manager to Hermes's own gateway, over
// Hermes's built-in OpenAI-compatible api_server platform (gateway/
// platforms/api_server.py, hermes/config.yaml's platforms.api_server
// block) — reached via the hermes-gateway K8s Service (k8s/company-ops.yaml)
// on port 8642, never exposed outside the cluster.
//
// A Discord-based bridge was considered first and rejected: Hermes's own
// Discord adapter ignores messages authored by its own bot identity
// (message.author == self._client.user), so posting with the same
// DISCORD_BOT_TOKEN Hermes itself uses would never surface a reply. The
// api_server endpoint is a normal synchronous HTTP request/response, no
// polling needed.
//
// This is a plain HTTP client with no cluster/docker/build capability of
// its own — it can only ask Hermes a question and get an answer back.
// Whatever Hermes decides to do as a result (e.g. call its own
// container_manager tools) is entirely Hermes's decision, made inside its
// own process with its own credentials — this tool has no visibility into
// or control over that.
import http from "node:http";
import readline from "node:readline";

const HERMES_API_HOST = process.env.HERMES_API_HOST || "hermes-gateway.company-ops.svc.cluster.local";
const HERMES_API_PORT = process.env.HERMES_API_PORT || "8642";
const API_SERVER_KEY = process.env.API_SERVER_KEY || "";
const base = `http://${HERMES_API_HOST}:${HERMES_API_PORT}`;

function postJson(path, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body));
    const request = http.request(`${base}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": data.length,
        ...(API_SERVER_KEY ? { Authorization: `Bearer ${API_SERVER_KEY}` } : {}),
      },
      timeout: timeoutMs,
    }, (response) => {
      let text = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { text += chunk; });
      response.on("end", () => {
        let parsed;
        try { parsed = text ? JSON.parse(text) : null; } catch { parsed = text; }
        if ((response.statusCode || 500) >= 400) {
          return reject(new Error(`hermes api_server ${response.statusCode}: ${text || response.statusMessage}`));
        }
        resolve(parsed);
      });
    });
    request.on("timeout", () => request.destroy(new Error(`hermes api_server request timed out after ${timeoutMs}ms`)));
    request.on("error", reject);
    request.write(data);
    request.end();
  });
}

function text(id, value) {
  return { jsonrpc: "2.0", id, result: { content: [{ type: "text", text: typeof value === "string" ? value : JSON.stringify(value, null, 2) }] } };
}
function fail(id, message) {
  return { jsonrpc: "2.0", id, error: { code: -32000, message } };
}
function send(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }

const tools = [
  {
    name: "hermes_ask",
    description: "Send a message to this Hermes instance's own OpenAI-compatible chat endpoint and return its reply synchronously. Hermes retains its own full tool access (terminal, container_manager, memory, etc) when processing the message — this tool only carries the question and the answer, it has no visibility into or control over what Hermes does to produce that answer.",
    inputSchema: {
      type: "object",
      required: ["message"],
      properties: {
        message: { type: "string", description: "The message to send to Hermes." },
        timeout_seconds: { type: "number", minimum: 5, maximum: 900, default: 120, description: "How long to wait for Hermes's reply before giving up." },
      },
      additionalProperties: false,
    },
  },
];

async function hermesAsk(message, timeoutSeconds) {
  if (!API_SERVER_KEY) {
    throw new Error("API_SERVER_KEY is not set in the environment — hermes-messenger-mcp cannot authenticate to Hermes's api_server");
  }
  const timeoutMs = Math.round((timeoutSeconds ?? 120) * 1000);
  const response = await postJson("/v1/chat/completions", {
    model: "hermes-agent",
    messages: [{ role: "user", content: message }],
    stream: false,
  }, timeoutMs);
  const reply = response?.choices?.[0]?.message?.content;
  if (typeof reply !== "string") {
    throw new Error(`unexpected response shape from Hermes api_server: ${JSON.stringify(response)}`);
  }
  return { reply };
}

async function call(name, args) {
  if (name === "hermes_ask") return hermesAsk(args.message, args.timeout_seconds);
  throw new Error(`unknown tool: ${name}`);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let request;
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") { send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: request.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "hermes-messenger", version: "0.1.0" } } }); continue; }
  if (request.method === "notifications/initialized") continue;
  if (request.method === "tools/list") { send({ jsonrpc: "2.0", id: request.id, result: { tools } }); continue; }
  if (request.method === "tools/call") {
    try { send(text(request.id, await call(request.params.name, request.params.arguments || {}))); }
    catch (error) { send(fail(request.id, error.message)); }
  }
}

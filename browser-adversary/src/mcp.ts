import { createInterface } from "node:readline";
import { runJob } from "./runner.js";

const allowed = new Set((process.env.ADVERSARY_ALLOWED_ORIGINS || "").split(",").map(value => value.trim()).filter(Boolean));
const authDir = process.env.ADVERSARY_AUTH_DIR || "/auth";
const tools = [{ name: "browser_adversary_test", description: "Run a bounded, source-blind UI test against an allowed live site. Prefer task mode; explore mode discovers candidate flows.", inputSchema: { type: "object", required: ["url"], properties: { url: { type: "string" }, task: { type: "string" }, mode: { type: "string", enum: ["task", "explore"], default: "task" }, max_steps: { type: "integer", minimum: 1, maximum: 30, default: 10 }, auth_profile: { type: "string", description: "Optional pre-mounted /auth/<name>.json browser state" } }, additionalProperties: false } }];

const send = (value: object) => process.stdout.write(`${JSON.stringify(value)}\n`);
const result = (id: number, value: object) => send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(value, null, 2) }] } });
const error = (id: number, message: string) => send({ jsonrpc: "2.0", id, error: { code: -32000, message } });

async function call(args: Record<string, unknown>): Promise<object> {
  const url = String(args.url || "");
  const origin = new URL(url).origin;
  if (allowed.size && !allowed.has(origin)) throw new Error(`origin is not allowed: ${origin}`);
  const profile = args.auth_profile ? String(args.auth_profile) : "";
  if (profile && !/^[a-zA-Z0-9_-]+$/.test(profile)) throw new Error("invalid auth_profile");
  return runJob({ url, mode: args.mode === "explore" ? "explore" : "task", task: args.task ? String(args.task) : undefined, max_steps: Number(args.max_steps || 10), storage_state: profile ? `${authDir}/${profile}.json` : undefined, output_dir: `/reports/${Date.now()}` });
}

const input = createInterface({ input: process.stdin });
for await (const line of input) {
  let request: { method?: string; id?: number; params?: { name?: string; arguments?: Record<string, unknown> } };
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "browser-adversary", version: "0.1.0" } } });
  else if (request.method === "notifications/initialized") continue;
  else if (request.method === "tools/list") send({ jsonrpc: "2.0", id: request.id, result: { tools } });
  else if (request.method === "tools/call") {
    try { result(request.id!, await call(request.params?.arguments || {})); } catch (err) { error(request.id!, String(err)); }
  }
}

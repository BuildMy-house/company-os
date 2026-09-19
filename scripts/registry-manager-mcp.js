#!/usr/bin/env node

// In-cluster registry inventory/cleanup. Talks to the registry:2 HTTP API
// (k8s/registry.yaml) over its in-cluster Service DNS name — no host access,
// no docker socket, no k3s ctr/sudo. Building and pushing new images stays
// outside the pod (same boundary container-manager-mcp.js already draws for
// container images) — this only lists/removes what is already pushed.
import http from "node:http";
import readline from "node:readline";

const REGISTRY_HOST = process.env.REGISTRY_HOST || "registry.company-ops.svc.cluster.local";
const REGISTRY_PORT = process.env.REGISTRY_PORT || "5000";
const base = `http://${REGISTRY_HOST}:${REGISTRY_PORT}`;

async function registry(path, options = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request(`${base}${path}`, {
      method: options.method || "GET",
      headers: { Accept: "application/vnd.docker.distribution.manifest.v2+json", ...(options.headers || {}) },
    }, (response) => {
      let text = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { text += chunk; });
      response.on("end", () => {
        let body;
        try { body = text ? JSON.parse(text) : null; } catch { body = text; }
        if ((response.statusCode || 500) >= 400) return reject(new Error(`${response.statusCode}: ${text || response.statusMessage}`));
        resolve({ body, headers: response.headers });
      });
    });
    request.on("error", reject);
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

function validateRepo(repo) {
  if (!/^[a-z0-9][a-z0-9._/-]*$/.test(repo)) throw new Error(`invalid repository name: ${repo}`);
  return repo;
}
function validateTag(tag) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(tag)) throw new Error(`invalid tag: ${tag}`);
  return tag;
}

const tools = [
  { name: "registry_list_repositories", description: "List every repository (image name) pushed to the in-cluster registry.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "registry_list_tags", description: "List tags pushed under one repository in the in-cluster registry.", inputSchema: { type: "object", required: ["repository"], properties: { repository: { type: "string" } }, additionalProperties: false } },
  { name: "registry_delete_tag", description: "Delete one tag from the in-cluster registry by its manifest digest. Does not reclaim disk until a garbage-collect pass runs.", inputSchema: { type: "object", required: ["repository", "tag"], properties: { repository: { type: "string" }, tag: { type: "string" } }, additionalProperties: false } },
];

async function listRepositories() {
  const { body } = await registry("/v2/_catalog?n=1000");
  return { repositories: body.repositories || [] };
}

async function listTags(repository) {
  validateRepo(repository);
  const { body } = await registry(`/v2/${repository}/tags/list`);
  return { repository, tags: body.tags || [] };
}

async function deleteTag(repository, tag) {
  validateRepo(repository);
  validateTag(tag);
  const { headers } = await registry(`/v2/${repository}/manifests/${tag}`, { method: "HEAD" });
  const digest = headers["docker-content-digest"];
  if (!digest) throw new Error(`no manifest digest found for ${repository}:${tag}`);
  await registry(`/v2/${repository}/manifests/${digest}`, { method: "DELETE" });
  return { repository, tag, digest, deleted: true, note: "disk is not reclaimed until the registry pod runs its garbage-collect command" };
}

async function call(name, args) {
  if (name === "registry_list_repositories") return listRepositories();
  if (name === "registry_list_tags") return listTags(args.repository);
  if (name === "registry_delete_tag") return deleteTag(args.repository, args.tag);
  throw new Error(`unknown tool: ${name}`);
}

const input = readline.createInterface({ input: process.stdin });
for await (const line of input) {
  let request;
  try { request = JSON.parse(line); } catch { continue; }
  if (request.method === "initialize") { send({ jsonrpc: "2.0", id: request.id, result: { protocolVersion: request.params?.protocolVersion || "2025-03-26", capabilities: { tools: {} }, serverInfo: { name: "registry-manager", version: "0.1.0" } } }); continue; }
  if (request.method === "notifications/initialized") continue;
  if (request.method === "tools/list") { send({ jsonrpc: "2.0", id: request.id, result: { tools } }); continue; }
  if (request.method === "tools/call") {
    try { send(text(request.id, await call(request.params.name, request.params.arguments || {}))); }
    catch (error) { send(fail(request.id, error.message)); }
  }
}

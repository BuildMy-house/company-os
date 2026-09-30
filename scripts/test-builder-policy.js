#!/usr/bin/env node
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const sha = "a".repeat(40);
const requests = [
  { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-03-26" } },
  { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} },
  { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "builder_build_and_push", arguments: { context_ref: `https://github.com/BuildMy-house/other.git#${sha}`, dockerfile_path: "Dockerfile", image_repo: "other", image_tag: "test" } } },
  { jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "build_company_os_image", arguments: { source_sha: "main" } } },
  { jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "build_company_os_image", arguments: { source_sha: sha, context_ref: `https://github.com/BuildMy-house/other.git#${sha}` } } },
];
const result = spawnSync(process.execPath, [fileURLToPath(new URL("./builder-manager-mcp.js", import.meta.url))], {
  env: { ...process.env, BUILDER_SCOPE: "company-os" },
  input: `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
  encoding: "utf8",
  timeout: 5000,
});
assert.equal(result.status, 0, result.stderr);
assert.ok(result.stdout.trim(), result.error?.message || result.stderr || "builder emitted no responses");
const replies = result.stdout.trim().split("\n").map((line) => JSON.parse(line));
assert.deepEqual(replies[1].result.tools.map(({ name }) => name), ["build_company_os_image"]);
assert.match(replies[2].error.message, /unknown tool/);
assert.match(replies[3].error.message, /full lowercase 40-character commit SHA/);
assert.match(replies[4].error.message, /only source_sha and timeout_seconds/);

const noScopeEnv = { ...process.env };
delete noScopeEnv.BUILDER_SCOPE;
const unscoped = spawnSync(process.execPath, [fileURLToPath(new URL("./builder-manager-mcp.js", import.meta.url))], {
  env: noScopeEnv,
  input: "",
  encoding: "utf8",
  timeout: 5000,
});
assert.notEqual(unscoped.status, 0, "builder must fail closed when its scope is missing");
assert.match(unscoped.stderr, /BUILDER_SCOPE must be explicitly set/);

const engineering = spawnSync(process.execPath, [fileURLToPath(new URL("./builder-manager-mcp.js", import.meta.url))], {
  env: { ...process.env, BUILDER_SCOPE: "engineering" },
  input: `${JSON.stringify(requests[0])}\n${JSON.stringify(requests[1])}\n`,
  encoding: "utf8",
  timeout: 5000,
});
assert.equal(engineering.status, 0, engineering.stderr);
assert.deepEqual(JSON.parse(engineering.stdout.trim().split("\n")[1]).result.tools.map(({ name }) => name), ["builder_build_and_push"]);
console.log("builder scope policy passed");

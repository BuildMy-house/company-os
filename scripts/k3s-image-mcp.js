#!/usr/bin/env node
'use strict';

// Host-side MCP server for the k3s containerd image store (`k3s ctr images`).
// This is a node-level/root privilege boundary, distinct from the K8s API
// that container-manager-mcp.js manages — building/importing images
// deliberately stays outside any pod (see that file's own comment), so this
// runs on the host and is wired into the project's .mcp.json, not shipped
// in Dockerfile.engineering. Each subcommand shells out via `sudo -n` to the
// exact scoped commands granted in /etc/sudoers.d/claude-k3s-image-import
// (import, ls) — rm/tag need their own NOPASSWD grant to work; until then
// they fail with a clear message instead of hanging on a password prompt.

const { spawnSync } = require('node:child_process');

function send(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }
function text(id, value) {
  return { jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }] } };
}
function fail(id, message) {
  return { jsonrpc: '2.0', id, error: { code: -32000, message } };
}

function sudoCtr(args, input) {
  const result = spawnSync('sudo', ['-n', 'k3s', 'ctr', ...args], {
    input,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
  if (result.error) throw new Error(result.error.message);
  const stderr = (result.stderr || '').trim();
  if (stderr.includes('a password is required') || stderr.includes('sudo:')) {
    throw new Error(
      `Not permitted without a password for: k3s ctr ${args.join(' ')}. ` +
      `Add a NOPASSWD sudoers grant for this subcommand (matching the existing ` +
      `/etc/sudoers.d/claude-k3s-image-import pattern) before this tool can run it.`
    );
  }
  if (result.status !== 0) {
    throw new Error(stderr || `k3s ctr ${args.join(' ')} exited ${result.status}`);
  }
  return (result.stdout || '').trim();
}

function validateImageRef(ref) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._/-]*:[a-zA-Z0-9._-]+$/.test(ref)) {
    throw new Error(`invalid image reference: ${ref}`);
  }
  return ref;
}

function listImages(filter) {
  const out = sudoCtr(['images', 'ls']);
  const lines = out.split('\n').filter(Boolean);
  const header = lines[0];
  const rows = lines.slice(1)
    .filter((line) => !filter || line.includes(filter))
    .map((line) => line.split(/\s{2,}/)[0]);
  return { header, images: rows };
}

function importFromDockerTag(dockerTag) {
  validateImageRef(dockerTag);
  const save = spawnSync('docker', ['save', dockerTag], { encoding: 'buffer', maxBuffer: 2 * 1024 * 1024 * 1024 });
  if (save.error) throw new Error(save.error.message);
  if (save.status !== 0) throw new Error((save.stderr || Buffer.from('')).toString('utf8') || `docker save ${dockerTag} failed`);
  const importOut = sudoCtr(['images', 'import', '-'], save.stdout);
  return { imported: dockerTag, output: importOut };
}

const tools = [
  { name: 'k3s_images_list', description: 'List images in the k3s containerd store, optionally filtered by a substring.', inputSchema: { type: 'object', properties: { filter: { type: 'string' } }, additionalProperties: false } },
  { name: 'k3s_image_import_from_docker', description: 'docker save <tag> piped into `k3s ctr images import -` — the standard build->deploy handoff for locally built images.', inputSchema: { type: 'object', required: ['docker_tag'], properties: { docker_tag: { type: 'string' } }, additionalProperties: false } },
  { name: 'k3s_image_rm', description: 'Remove one image reference from the k3s containerd store. Requires an additional NOPASSWD sudoers grant beyond the default import/ls one.', inputSchema: { type: 'object', required: ['image'], properties: { image: { type: 'string' } }, additionalProperties: false } },
  { name: 'k3s_image_tag', description: 'Tag an existing k3s containerd image under a new reference. Requires an additional NOPASSWD sudoers grant beyond the default import/ls one.', inputSchema: { type: 'object', required: ['source', 'target'], properties: { source: { type: 'string' }, target: { type: 'string' } }, additionalProperties: false } },
];

function call(name, args) {
  if (name === 'k3s_images_list') return listImages(args.filter);
  if (name === 'k3s_image_import_from_docker') return importFromDockerTag(args.docker_tag);
  if (name === 'k3s_image_rm') return { removed: sudoCtr(['images', 'rm', validateImageRef(args.image)]) };
  if (name === 'k3s_image_tag') return { tagged: sudoCtr(['images', 'tag', validateImageRef(args.source), validateImageRef(args.target)]) };
  throw new Error(`unknown tool: ${name}`);
}

const rl = require('node:readline').createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  const { id, method, params } = msg;
  try {
    if (method === 'initialize') {
      send({ jsonrpc: '2.0', id, result: { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'k3s-image-mcp', version: '1.0.0' } } });
    } else if (method === 'tools/list') {
      send({ jsonrpc: '2.0', id, result: { tools } });
    } else if (method === 'tools/call') {
      send(text(id, call(params.name, params.arguments || {})));
    } else if (id !== undefined) {
      send({ jsonrpc: '2.0', id, result: {} });
    }
  } catch (err) {
    if (id !== undefined) send(fail(id, err.message));
  }
});

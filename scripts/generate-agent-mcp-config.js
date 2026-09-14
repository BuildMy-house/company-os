#!/usr/bin/env node
'use strict';

/**
 * generate-agent-mcp-config.js
 *
 * Writes/merges the "axiom" and "infisical" MCP server entries into the
 * user-scope configs for Claude Code (~/.claude.json) and OpenCode
 * (~/.config/opencode/opencode.json), reading credentials from the current
 * process environment (populated by fetch-infisical-secrets.js). Existing
 * keys/servers in those files are preserved — only "axiom"/"infisical" are
 * added or overwritten. Run at container start, after secrets are fetched;
 * these files are never committed to a repo.
 *
 * Codex is configured separately via `codex mcp add` (see entrypoint), since
 * codex writes TOML and its own CLI already merges idempotently.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const HOME = os.homedir();

function readJson(filePath) {
  if (!fs.existsSync(filePath)) return {};
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (err) {
    console.error(`[generate-agent-mcp-config] WARN: could not parse ${filePath} (${err.message}); starting fresh`);
    return {};
  }
}

function writeJson(filePath, data) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n');
}

function buildServers() {
  const servers = {};

  if (process.env.AXIOM_TOKEN) {
    servers.axiom = {
      kind: 'stdio',
      command: ['npx', '-y', 'mcp-server-axiom'],
      environment: {
        AXIOM_TOKEN: process.env.AXIOM_TOKEN,
        AXIOM_ORG_ID: process.env.AXIOM_ORG_ID || '',
        AXIOM_URL: process.env.AXIOM_ENDPOINT || 'https://api.axiom.co',
      },
    };
  }

  if (process.env.INFISICAL_UNIVERSAL_AUTH_CLIENT_ID) {
    servers.infisical = {
      kind: 'stdio',
      command: ['npx', '-y', '--legacy-peer-deps', '@infisical/mcp'],
      environment: {
        INFISICAL_HOST_URL: process.env.INFISICAL_HOST_URL || 'https://app.infisical.com',
        INFISICAL_AUTH_METHOD: 'universal-auth',
        INFISICAL_UNIVERSAL_AUTH_CLIENT_ID: process.env.INFISICAL_UNIVERSAL_AUTH_CLIENT_ID,
        INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET: process.env.INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET,
      },
    };
  }

  if (process.env.NEON_API_KEY) {
    servers.neon = {
      kind: 'http',
      url: 'https://mcp.neon.tech/mcp',
      headers: { Authorization: `Bearer ${process.env.NEON_API_KEY}` },
    };
  }

  if (process.env.CLOUDFLARE_API_TOKEN) {
    servers['cloudflare-bindings'] = {
      kind: 'http',
      url: 'https://bindings.mcp.cloudflare.com/mcp',
      headers: { Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}` },
    };
  }

  if (process.env.STEWARD_TOKEN) {
    servers.steward = {
      kind: 'http',
      url: process.env.STEWARD_URL || 'https://buildmyhouse.stewardacs.xyz/mcp/sse',
      headers: { Authorization: `Bearer ${process.env.STEWARD_TOKEN}` },
    };
  }

  // ai-cli-mcp: registered without an explicit env override (matches how
  // Dockerfile.engineering already does `claude mcp add ai-cli-mcp` and
  // `codex mcp add ai-cli-mcp` — no --env flags, so it inherits the
  // container's process environment naturally) — lets an agent recursively
  // dispatch sub-tasks to opencode/codex/claude through the same router
  // this container itself exposes.
  servers['ai-cli'] = {
    kind: 'stdio',
    command: ['npx', '-y', 'ai-cli-mcp@latest'],
    environment: null,
  };

  return servers;
}

function toClaudeServer(server) {
  if (server.kind === 'http') {
    return { type: 'http', url: server.url, headers: server.headers };
  }
  const entry = { command: server.command[0], args: server.command.slice(1) };
  if (server.environment) entry.env = server.environment;
  return entry;
}

function toOpencodeServer(server) {
  if (server.kind === 'http') {
    return { type: 'remote', url: server.url, headers: server.headers, enabled: true };
  }
  const entry = { type: 'local', command: server.command, enabled: true };
  if (server.environment) entry.environment = server.environment;
  return entry;
}

function updateClaude(servers) {
  const filePath = path.join(HOME, '.claude.json');
  const config = readJson(filePath);
  config.mcpServers = config.mcpServers || {};
  for (const [name, server] of Object.entries(servers)) {
    config.mcpServers[name] = toClaudeServer(server);
  }
  writeJson(filePath, config);
  console.error(`[generate-agent-mcp-config] wrote ${Object.keys(servers).join(', ')} to ${filePath}`);
}

// Axiom token/tool-call usage metrics for OpenCode — see
// scripts/opencode-plugins/axiom-usage.js (registered globally, all repos,
// not per-project). The plugin itself no-ops without AXIOM_TOKEN, so this
// just needs the path present in opencode.json's `plugin` array.
const AXIOM_OPENCODE_PLUGIN_PATH = '/opt/company-ops/scripts/opencode-plugins/axiom-usage.js';

function updateOpencodePlugins(config) {
  if (!process.env.AXIOM_TOKEN) return;
  config.plugin = config.plugin || [];
  const already = config.plugin.some((entry) =>
    (Array.isArray(entry) ? entry[0] : entry) === AXIOM_OPENCODE_PLUGIN_PATH);
  if (!already) config.plugin.push(AXIOM_OPENCODE_PLUGIN_PATH);
}

function updateOpencode(servers) {
  const filePath = path.join(HOME, '.config', 'opencode', 'opencode.json');
  const config = readJson(filePath);
  config.mcp = config.mcp || {};
  for (const [name, server] of Object.entries(servers)) {
    config.mcp[name] = toOpencodeServer(server);
  }
  updateOpencodePlugins(config);
  writeJson(filePath, config);
  console.error(`[generate-agent-mcp-config] wrote ${Object.keys(servers).join(', ')} to ${filePath}`);
}

function main() {
  const servers = buildServers();
  if (Object.keys(servers).length === 0) {
    console.error('[generate-agent-mcp-config] no credentials in environment; nothing to write');
    return;
  }
  updateClaude(servers);
  updateOpencode(servers);
}

main();

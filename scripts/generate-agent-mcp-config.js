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
      type: 'local',
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
      type: 'local',
      command: ['npx', '-y', '--legacy-peer-deps', '@infisical/mcp'],
      environment: {
        INFISICAL_HOST_URL: process.env.INFISICAL_HOST_URL || 'https://app.infisical.com',
        INFISICAL_AUTH_METHOD: 'universal-auth',
        INFISICAL_UNIVERSAL_AUTH_CLIENT_ID: process.env.INFISICAL_UNIVERSAL_AUTH_CLIENT_ID,
        INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET: process.env.INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET,
      },
    };
  }

  return servers;
}

function toClaudeServer(server) {
  return {
    command: server.command[0],
    args: server.command.slice(1),
    env: server.environment,
  };
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

function updateOpencode(servers) {
  const filePath = path.join(HOME, '.config', 'opencode', 'opencode.json');
  const config = readJson(filePath);
  config.mcp = config.mcp || {};
  for (const [name, server] of Object.entries(servers)) {
    config.mcp[name] = { ...server, enabled: true };
  }
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

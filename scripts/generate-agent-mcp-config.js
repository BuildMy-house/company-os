#!/usr/bin/env node
'use strict';

/**
 * generate-agent-mcp-config.js
 *
 * Writes/merges MCP server entries into the
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

  // Official Axiom-hosted MCP (tools: queryApl, listDatasets,
  // getDatasetInfoAndSchema). Query access uses its OWN read-only credential,
  // AXIOM_QUERY_TOKEN — never AXIOM_TOKEN, which is the ingest token the
  // telemetry plugins write with. Credentials stay as env placeholders so
  // they are never written into the generated files.
  if (process.env.AXIOM_QUERY_TOKEN && process.env.AXIOM_ORG_ID) {
    servers.axiom = {
      kind: 'http',
      url: 'https://mcp.axiom.co/mcp',
      headers: {
        Authorization: 'Bearer {env:AXIOM_QUERY_TOKEN}',
        'x-axiom-org-id': '{env:AXIOM_ORG_ID}',
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

  if (process.env.HIVE_URL && process.env.HIVE_AGENT_ID) {
    servers.hive = {
      kind: 'stdio',
      command: ['node', '/opt/company-ops/scripts/hive-member-mcp.js'],
      environment: {
        HIVE_URL: process.env.HIVE_URL,
        HIVE_AGENT_ID: process.env.HIVE_AGENT_ID,
      },
    };
  }

  if (process.env.CLOUDFLARE_API_TOKEN) {
    servers['cloudflare-bindings'] = {
      kind: 'http',
      url: 'https://bindings.mcp.cloudflare.com/mcp',
      headers: { Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}` },
    };
  }

  const stewardToken = process.env.STEWARD_TOKEN;
  if (stewardToken) {
    servers.steward = {
      kind: 'http',
      url: process.env.STEWARD_MCP_URL || process.env.STEWARD_URL || 'https://buildmyhouse.stewardacs.xyz/mcp/sse',
      // Keep credentials out of the generated files; Claude/OpenCode resolve
      // this environment placeholder when they launch the MCP.
      headers: { Authorization: 'Bearer {env:STEWARD_TOKEN}' },
    };
  }

  // Route worker calls through the policy wrapper: only OpenCode workers are
  // allowed, and bare opencode/* models get the required oc- prefix.
  servers['ai-cli'] = {
    kind: 'stdio',
    command: ['node', '/opt/company-ops/scripts/ai-cli-mcp-policy.js'],
    environment: null,
  };

  // Claude's manager may request safe local deployment operations. OpenCode
  // workers deliberately do not receive this capability. Gated on the
  // pod's default in-cluster ServiceAccount token, which is what these two
  // scripts actually authenticate with.
  if (fs.existsSync('/var/run/secrets/kubernetes.io/serviceaccount/token')) {
    // Same kubernetes-mcp-server Hermes already runs (hermes/config.yaml's
    // k8s_deployment entry) and the same "company-ops" ServiceAccount/RBAC
    // (k8s/rbac.yaml) -- gives the engineering-agent itself live cluster
    // visibility for docs/DEV-CLUSTER-GATE.md's runbook (poller
    // CronJob/Job status and logs, buildmyhouse-dev pods/deploy-state
    // configmap) instead of only the scoped container-manager/
    // builder-manager wrappers. --disable-destructive is a second layer on
    // top of the RBAC grant, same as Hermes's copy. Kept out of the
    // OpenCode skip-list below for the same reason container-manager is
    // Claude-manager-only: this SA can create/patch/delete
    // Deployments/ReplicaSets in company-ops, which is more than a worker
    // needs for read-only debugging.
    servers['kubernetes'] = {
      kind: 'stdio',
      command: ['npx', '-y', 'kubernetes-mcp-server@latest', '--disable-destructive', '--log-file', 'stderr', '--cluster-provider', 'in-cluster'],
      environment: {
        KUBERNETES_SERVICE_HOST: process.env.KUBERNETES_SERVICE_HOST || '10.43.0.1',
        KUBERNETES_SERVICE_PORT: process.env.KUBERNETES_SERVICE_PORT || '443',
      },
    };
    servers['container-manager'] = {
      kind: 'stdio',
      command: ['node', '/opt/company-ops/scripts/container-manager-mcp.js'],
      environment: null,
    };
    servers['registry-manager'] = {
      kind: 'stdio',
      command: ['node', '/opt/company-ops/scripts/registry-manager-mcp.js'],
      environment: null,
    };
    // Bridges Claude/agent-manager to this Hermes instance's own
    // OpenAI-compatible api_server endpoint (hermes/config.yaml
    // platforms.api_server, k8s/company-ops.yaml's hermes-gateway Service).
    // Same OpenCode skip-list treatment as container-manager/registry-manager
    // above: talking to Hermes can indirectly cause Hermes to act (e.g. call
    // its own container_manager tools), so it stays Claude-manager only.
    servers['hermes-messenger'] = {
      kind: 'stdio',
      command: ['node', '/opt/company-ops/scripts/hermes-messenger-mcp.js'],
      environment: null,
    };
  }

  // Ephemeral, rootless-BuildKit-based build+push (k8s/builder-rbac.yaml).
  // Unlike container-manager/registry-manager/hermes-messenger above, this
  // one is deliberately NOT added to the OpenCode skip-list below — the
  // Job's own RBAC is already narrowly scoped (create/watch/delete Jobs it
  // owns only, create/delete (never get/list/watch) only its own ephemeral
  // per-build git-token Secret, no Deployment access or ability to read
  // back any Secret), so OpenCode workers dispatched from
  // engineering-manager can trigger builds directly too.
  //
  // Gated on its OWN two prerequisites, not the generic in-cluster SA check
  // above: builder-manager-mcp.js authenticates with a distinct bound token
  // at BUILDER_MANAGER_TOKEN_PATH (k8s/builder-rbac.yaml's
  // "builder-manager-token" Secret, mounted separately from the default SA
  // token — see that script's header), and it must actually be present in
  // this image (COPY'd by Dockerfile.engineering) to be launchable at all.
  // A runner should never advertise a tool it cannot launch — checking both
  // here, at generation time, is the structural fix; scripts/
  // check-advertised-mcp-tools.js is the runnable check that catches it if
  // this guard is ever bypassed or a similar gap is introduced elsewhere.
  const BUILDER_MANAGER_TOKEN_PATH = process.env.BUILDER_MANAGER_TOKEN_PATH || '/var/run/secrets/builder-manager/token';
  const BUILDER_MANAGER_SCRIPT_PATH = '/opt/company-ops/scripts/builder-manager-mcp.js';
  const hasBuilderManagerToken = fs.existsSync(BUILDER_MANAGER_TOKEN_PATH);
  const hasBuilderManagerScript = fs.existsSync(BUILDER_MANAGER_SCRIPT_PATH);
  if (hasBuilderManagerToken && hasBuilderManagerScript) {
    servers['builder-manager'] = {
      kind: 'stdio',
      command: ['node', BUILDER_MANAGER_SCRIPT_PATH],
      environment: { BUILDER_SCOPE: 'engineering' },
    };
  } else if (hasBuilderManagerToken && !hasBuilderManagerScript) {
    console.error(`[generate-agent-mcp-config] WARN: builder-manager-token present but ${BUILDER_MANAGER_SCRIPT_PATH} is missing from this image; not advertising builder-manager`);
  }

  return servers;
}

function toClaudeServer(server) {
  if (server.kind === 'http') {
    const headers = Object.fromEntries(Object.entries(server.headers || {}).map(([key, value]) => [key, value.replace(/\{env:([^}]+)\}/g, '${$1}')]));
    return { type: 'http', url: server.url, headers };
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
    if (name === 'container-manager' || name === 'registry-manager' || name === 'hermes-messenger' || name === 'kubernetes') continue;
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

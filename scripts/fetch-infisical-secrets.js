#!/usr/bin/env node
'use strict';

/**
 * fetch-infisical-secrets.js
 *
 * Authenticates to Infisical via Universal Auth and prints every secret in
 * the given project/environment as dotenv lines (KEY=value) to stdout, for
 * an entrypoint script to `source`. Zero external dependencies: Node 18+
 * built-in fetch only.
 *
 * Usage:
 *   node fetch-infisical-secrets.js > secrets.env
 *
 * Required environment variables:
 *   INFISICAL_HOST_URL                    e.g. https://eu.infisical.com
 *   INFISICAL_UNIVERSAL_AUTH_CLIENT_ID
 *   INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET
 *   INFISICAL_PROJECT_ID
 * Optional:
 *   INFISICAL_ENV                         default: dev
 *   INFISICAL_SECRET_PATH                 default: /
 */

const HOST_URL = process.env.INFISICAL_HOST_URL;
const CLIENT_ID = process.env.INFISICAL_UNIVERSAL_AUTH_CLIENT_ID;
const CLIENT_SECRET = process.env.INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET;
const PROJECT_ID = process.env.INFISICAL_PROJECT_ID;
const ENVIRONMENT = process.env.INFISICAL_ENV || 'dev';
const SECRET_PATH = process.env.INFISICAL_SECRET_PATH || '/';

function dotenvEscape(value) {
  // Wrap in double quotes and escape embedded quotes/backslashes/newlines so
  // multiline secrets (e.g. PEM keys) survive a `source` in bash.
  return '"' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n') + '"';
}

async function login() {
  const res = await fetch(`${HOST_URL}/api/v1/auth/universal-auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ clientId: CLIENT_ID, clientSecret: CLIENT_SECRET }),
  });
  const data = await res.json();
  if (!res.ok || !data.accessToken) {
    throw new Error(`Infisical login failed (HTTP ${res.status}): ${data.message || JSON.stringify(data)}`);
  }
  return data.accessToken;
}

async function listSecrets(accessToken) {
  const url = new URL(`${HOST_URL}/api/v3/secrets/raw`);
  url.searchParams.set('workspaceId', PROJECT_ID);
  url.searchParams.set('environment', ENVIRONMENT);
  url.searchParams.set('secretPath', SECRET_PATH);

  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(`Infisical secrets fetch failed (HTTP ${res.status}): ${data.message || JSON.stringify(data)}`);
  }
  return data.secrets || [];
}

async function main() {
  for (const [name, val] of [
    ['INFISICAL_HOST_URL', HOST_URL],
    ['INFISICAL_UNIVERSAL_AUTH_CLIENT_ID', CLIENT_ID],
    ['INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET', CLIENT_SECRET],
    ['INFISICAL_PROJECT_ID', PROJECT_ID],
  ]) {
    if (!val) {
      console.error(`[fetch-infisical-secrets] ERROR: ${name} is not set`);
      process.exit(1);
    }
  }

  try {
    const accessToken = await login();
    const secrets = await listSecrets(accessToken);
    for (const secret of secrets) {
      process.stdout.write(`${secret.secretKey}=${dotenvEscape(secret.secretValue)}\n`);
    }
    console.error(`[fetch-infisical-secrets] OK: fetched ${secrets.length} secret(s) from ${ENVIRONMENT}${SECRET_PATH}`);
  } catch (err) {
    console.error(`[fetch-infisical-secrets] ERROR: ${err.message}`);
    process.exit(1);
  }
}

if (require.main === module) {
  main();
}

module.exports = { login, listSecrets };

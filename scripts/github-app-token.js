#!/usr/bin/env node
'use strict';

/**
 * github-app-token.js
 *
 * Generates a short-lived GitHub App installation access token via RS256 JWT.
 * Zero external dependencies: uses Node.js 18+ built-ins (crypto, fs, fetch).
 *
 * Usage:
 *   node github-app-token.js
 *   node github-app-token.js --check
 *
 * Environment variables (with sensible defaults):
 *   GITHUB_APP_ID               default: 4857525
 *   GITHUB_APP_INSTALLATION_ID  default: 159686448
 *   GITHUB_APP_PRIVATE_KEY_PATH default: /etc/github/app-private-key.pem
 */

const fs = require('fs');
const crypto = require('crypto');

const APP_ID = process.env.GITHUB_APP_ID || '4857525';
const INSTALLATION_ID = process.env.GITHUB_APP_INSTALLATION_ID || '159686448';
const DEFAULT_KEY_PATHS = [
  process.env.GITHUB_APP_PRIVATE_KEY_PATH,
  '/etc/github/buildmyhouse-engineering-app.pem',
  '/etc/github/app-private-key.pem',
  '/root/.ssh/buildmyhouse-engineering-app.pem',
  '/workspace/house_designer/certs/buildmyhouse-engineering-app.pem',
  'certs/buildmyhouse-engineering-app.pem',
  '../certs/buildmyhouse-engineering-app.pem',
].filter(Boolean);

function findPrivateKey() {
  for (const p of DEFAULT_KEY_PATHS) {
    if (fs.existsSync(p)) {
      try {
        const content = fs.readFileSync(p, 'utf8');
        if (content.includes('BEGIN') && content.includes('PRIVATE KEY')) {
          return { path: p, pem: content };
        }
      } catch {
        // ignore unreadable path and continue searching
      }
    }
  }
  return null;
}

function base64url(input) {
  return Buffer.from(input).toString('base64url');
}

function buildJwt(appId, privateKeyPem) {
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: 'RS256', typ: 'JWT' };
  const payload = {
    iat: now - 60, // clock drift buffer
    exp: now + (9 * 60), // expires in 9 minutes (GitHub max is 10 min)
    iss: appId,
  };

  const encodedHeader = base64url(JSON.stringify(header));
  const encodedPayload = base64url(JSON.stringify(payload));
  const message = `${encodedHeader}.${encodedPayload}`;

  const signer = crypto.createSign('RSA-SHA256');
  signer.update(message);
  const signature = signer.sign(privateKeyPem, 'base64url');

  return `${message}.${signature}`;
}

async function getInstallationToken({ appId, installationId, privateKeyPem }) {
  const jwt = buildJwt(appId, privateKeyPem);
  const url = `https://api.github.com/app/installations/${installationId}/access_tokens`;

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 15000);

  try {
    const res = await fetch(url, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Authorization': `Bearer ${jwt}`,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'buildmyhouse-engineering-app',
        'X-GitHub-Api-Version': '2022-11-28',
      },
    });

    const bodyText = await res.text();
    let data;
    try {
      data = JSON.parse(bodyText);
    } catch {
      throw new Error(`Non-JSON response from GitHub (HTTP ${res.status}): ${bodyText.slice(0, 200)}`);
    }

    if (!res.ok) {
      const msg = data.message || `HTTP ${res.status}`;
      throw new Error(`GitHub API error (${res.status}): ${msg}`);
    }

    if (!data.token) {
      throw new Error('No token field found in GitHub response');
    }

    return {
      token: data.token,
      expiresAt: data.expires_at,
      permissions: data.permissions,
    };
  } finally {
    clearTimeout(timeoutId);
  }
}

async function main() {
  const args = process.argv.slice(2);
  const isCheck = args.includes('--check');

  if (args.includes('--help') || args.includes('-h')) {
    console.log('Usage: github-app-token.js [--check]');
    console.log('');
    console.log('Generates a GitHub App installation access token and prints it to stdout.');
    console.log('Options:');
    console.log('  --check    Validate token generation without printing the token');
    process.exit(0);
  }

  const keyInfo = findPrivateKey();
  if (!keyInfo) {
    console.error(`[github-app-token] ERROR: Private key not found. Checked: ${DEFAULT_KEY_PATHS.join(', ')}`);
    process.exit(1);
  }

  try {
    const result = await getInstallationToken({
      appId: APP_ID,
      installationId: INSTALLATION_ID,
      privateKeyPem: keyInfo.pem,
    });

    if (isCheck) {
      console.log(`[github-app-token] OK: Token minted successfully (expires: ${result.expiresAt})`);
      process.exit(0);
    }

    // Output ONLY the token to stdout
    process.stdout.write(result.token + '\n');
  } catch (err) {
    console.error(`[github-app-token] ERROR: ${err.message}`);
    process.exit(1);
  }
}

if (require.main === module) {
  main();
}

module.exports = {
  buildJwt,
  getInstallationToken,
  findPrivateKey,
};

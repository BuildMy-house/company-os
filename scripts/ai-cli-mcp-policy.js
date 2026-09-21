#!/usr/bin/env node

import { spawn } from 'node:child_process';
import readline from 'node:readline';

function workerModel(model) {
  if (model === 'opencode' || model?.startsWith('oc-opencode/')) return model;
  if (model?.startsWith('opencode/')) return `oc-${model}`;
  throw new Error('Worker dispatches must use OpenCode (oc-opencode/<provider/model>).');
}

if (process.argv[2] === '--self-test') {
  console.assert(workerModel('opencode/mimo-v2.5-free') === 'oc-opencode/mimo-v2.5-free');
  console.assert(workerModel('oc-opencode/mimo-v2.5-free') === 'oc-opencode/mimo-v2.5-free');
  console.assert(workerModel('opencode') === 'opencode');
  let rejected = false;
  try { workerModel('sonnet'); } catch { rejected = true; }
  console.assert(rejected);
  process.exit(0);
}

const upstream = spawn('npx', ['-y', 'ai-cli-mcp@latest'], {
  stdio: ['pipe', 'pipe', 'inherit'],
  env: process.env,
});

const send = (message) => process.stdout.write(`${JSON.stringify(message)}\n`);
const input = readline.createInterface({ input: process.stdin });

input.on('line', (line) => {
  let message;
  try {
    message = JSON.parse(line);
    if (message.method === 'tools/call' && message.params?.name === 'run') {
      const args = { ...(message.params.arguments || {}) };
      args.model = workerModel(args.model);
      message.params.arguments = args;
    }
    upstream.stdin.write(`${JSON.stringify(message)}\n`);
  } catch (error) {
    send({ jsonrpc: '2.0', id: message?.id ?? null, error: { code: -32602, message: error.message } });
  }
});

upstream.stdout.pipe(process.stdout);
upstream.on('exit', (code, signal) => process.exit(code ?? (signal ? 1 : 0)));

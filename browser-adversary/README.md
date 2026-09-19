# buildmy.house browser adversary

Black-box UI testing only. The container receives a URL and a test job; it
does not receive a repository checkout or source code.

Task mode is the default and produces the most useful findings. Exploration
mode is bounded discovery: it follows visible links/buttons, records browser
errors, and asks the model to identify suspicious states. Use it to discover
candidate flows, then turn good discoveries into explicit tasks.

## Local run

```bash
npm install
OPENAI_API_KEY=... npm run run -- example.job.json
```

The API must be OpenAI-compatible. Optional variables:

```text
OPENAI_BASE_URL=https://api.openai.com/v1
ADVERSARY_MODEL=gpt-5-mini
```

Credentials for the target site must be supplied through the job's
`storage_state` path or a pre-authenticated test URL. Do not put passwords in
the job file or report bundle.

## Container run

```bash
docker build -t buildmy-house/browser-adversary .
docker run --rm \
  -e OPENAI_API_KEY \
  -v "$PWD/example.job.json:/job.json:ro" \
  -v "$PWD/reports:/reports" \
  buildmy-house/browser-adversary /job.json
```

The result is written to `/reports/report.json` and `/reports/report.md`.
The runner is intentionally read-only toward the application: it never
submits destructive actions and only allows the model to use browser actions.

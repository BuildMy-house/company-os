# Infisical Secrets Infrastructure Setup

This document describes how to set up Infisical for BuildMyHouse company operations secrets injection. Infisical handles secure credential provisioning at container startup—no secrets stored in `.env` files or version control.

## Overview

**Goal:** Inject all secrets into containers at runtime via Infisical, eliminating secrets from `.env`, code, and git.

**Architecture:**
- **Hermes Gateway Container** reads secrets from `/secrets/hermes/*` (operational role)
- **Engineering Container** reads secrets from `/secrets/infra/*` (infrastructure-only, no Hermes access)
- All other secrets are infra-only (GitHub, R2, Anthropic, LM models)

**Prerequisites:**
- Infisical account (create at https://infisical.com)
- Docker + docker-compose installed locally
- Access to BuildMyHouse GitHub organization

---

## Phase 1: Create Infisical Account & Projects

### 1.1 Create Infisical Organization Account

1. Go to https://infisical.com and sign up (or log in if you have an account)
2. Create a new organization for BuildMyHouse
3. Note your **Organization ID** and **workspace URL** (e.g., `https://app.infisical.com/org/my-org-id`)

### 1.2 Create Two Projects

Create two separate Infisical projects to enforce the access boundary:

**Project A: `buildmy-house-ops`**
- Scope: Hermes gateway, company-ops agent, operational secrets
- Folders: `/secrets/hermes/`
- Access: Hermes machine identity only

**Project B: `buildmy-house-infra`**
- Scope: Infrastructure automation, engineering container, infra-critical secrets
- Folders: `/secrets/infra/`
- Access: Engineering machine identity only (no Hermes access)

---

## Phase 2: Configure Machine Identities

Infisical uses **Machine Identities** to authenticate containers without human credentials.

### 2.1 Create Machine Identity for Hermes Gateway

**In `buildmy-house-ops` project:**

1. Go to **Machine Identities** → **Add Machine Identity**
2. Name: `hermes-gateway-prod` (or `hermes-gateway-dev` for local)
3. Configure access:
   - **Environment:** Production (or Development)
   - **Permissions:**
     - Folder: `/secrets/hermes/`
     - Action: Read only
4. Click **Create**
5. From the credential type dropdown, select **Access Token** (recommended for containers)
6. Generate a token — this becomes your `INFISICAL_TOKEN` for the Hermes container

**Save this token securely** (never commit to git). You'll set it as an environment variable when starting the container.

### 2.2 Create Machine Identity for Engineering Container

**In `buildmy-house-infra` project:**

1. Go to **Machine Identities** → **Add Machine Identity**
2. Name: `engineering-manager-prod` (or `engineering-manager-dev` for local)
3. Configure access:
   - **Environment:** Production (or Development)
   - **Permissions:**
     - Folder: `/secrets/infra/`
     - Action: Read only
4. Click **Create**
5. From the credential type dropdown, select **Access Token**
6. Generate a token — this becomes your `INFISICAL_TOKEN` for the engineering container

**Save this token securely.**

---

## Phase 3: Populate Secrets in Infisical

### 3.1 Tier 1: Hermes-Readable Secrets (`buildmy-house-ops` → `/secrets/hermes/`)

1. In the **buildmy-house-ops** project, create folder `/secrets/hermes/`
2. Add the following secrets as individual entries:

| Secret Name | Value | Source |
|---|---|---|
| `discord_bot_token` | Discord bot token (from Discord Developer Portal) | Discord App → Bot → Token |
| `discord_allowed_users` | CSV of Discord user IDs | Discord → User Settings → Copy User ID |
| `discord_allowed_channels` | CSV of Discord channel IDs | Discord → Right-click channel → Copy ID |
| `discord_announce_channel` | Announcement channel ID | Discord → Right-click channel → Copy ID |
| `discord_hil_channel` | Human Interface outbound (#hil) channel ID | Discord → Right-click channel → Copy ID |
| `discord_finance_channel` | Finance requests (#finance) channel ID | Discord → Right-click channel → Copy ID |
| `discord_dm_user` | Direct message recipient user ID | Discord → User Settings → Copy User ID |
| `discord_daily_prompt` | Hermes daily briefing prompt | Internal documentation |
| `discord_investor_prompt` | Hermes investor query prompt | Internal documentation |
| `discord_questions_prompt` | Hermes Q&A prompt | Internal documentation |
| `nous_api_key` | Nous Research API key | Nous Research → API Keys |
| `postgres_password` | Root postgres password | Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(20))"` |
| `company_password` | Hermes company role password | Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(20))"` |
| `observer_password` | Analytics observer role password | Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(20))"` |
| `analytics_password` | Analytics read-only role password | Generate: `python3 -c "import secrets; print(secrets.token_urlsafe(20))"` |

### 3.2 Tier 2: Infra-Only Secrets (`buildmy-house-infra` → `/secrets/infra/`)

1. In the **buildmy-house-infra** project, create folder `/secrets/infra/`
2. Add the following secrets as individual entries:

| Secret Name | Value | Source |
|---|---|---|
| `github_token` | GitHub personal access token (or leave empty if using GitHub App auth) | GitHub → Settings → Developer Settings → Personal Access Tokens |
| `r2_endpoint` | Cloudflare R2 endpoint | Cloudflare → R2 → Bucket details |
| `r2_access_key_id` | R2 API token access key | Cloudflare → R2 → API Tokens |
| `r2_secret_access_key` | R2 API token secret | Cloudflare → R2 → API Tokens |
| `slack_webhook_url` | Slack webhook for notifications | Slack App → Incoming Webhooks → Create New Webhook |
| `anthropic_api_key` | Claude API key (for engineering-manager headless CLI) | Anthropic → API Keys → Create new |
| `opencode_zen_api_key` | OpenCode Zen provider API key | OpenCode → Account → API Keys |
| `tokenrouter_api_key` | TokenRouter model aggregation API key | TokenRouter → Dashboard → API Keys |
| `axiom_token` | Axiom analytics read-only API token | Axiom → Settings → API Tokens |
| `axiom_org_id` | Axiom organization ID | Axiom → Settings → Organization |

**Note on naming:** Infisical path hierarchies use `/` separators. A secret at `/secrets/infra/r2_access_key_id` is fetched as environment variable `R2_ACCESS_KEY_ID` (uppercase, underscore-separated).

---

## Phase 4: Set Up Docker Environment

### 4.1 Configure Entrypoints

The entrypoint scripts are already configured to inject Infisical secrets:
- `company-ops/scripts/entrypoint.sh` (Hermes gateway)
- `company-ops/scripts/engineering-entrypoint.sh` (engineering container)

Both scripts check for `INFISICAL_TOKEN` environment variable and fetch secrets if present.

### 4.2 Start Container with Infisical Token

**For Hermes Gateway (using hermes-gateway token):**

```bash
INFISICAL_TOKEN=<hermes-gateway-machine-identity-token> \
docker compose up company-ops
```

**For Engineering Container (using engineering-manager token):**

```bash
INFISICAL_TOKEN=<engineering-manager-machine-identity-token> \
docker compose up engineering
```

**For Both Services:**

```bash
# Set environment variable before compose
export INFISICAL_HERMES_TOKEN=<hermes-gateway-machine-identity-token>
export INFISICAL_ENGINEERING_TOKEN=<engineering-manager-machine-identity-token>

# Option 1: Use shell expansion in docker-compose
INFISICAL_TOKEN=$INFISICAL_HERMES_TOKEN docker compose up company-ops &
INFISICAL_TOKEN=$INFISICAL_ENGINEERING_TOKEN docker compose up engineering &
wait
```

Or edit `docker-compose.yml` to include INFISICAL_TOKEN in the `environment:` section (not recommended for production—tokens should be provided at runtime).

### 4.3 Verify Secrets Were Injected

Check container logs:

```bash
# Hermes gateway
docker compose logs company-ops | grep infisical

# Engineering
docker compose logs engineering | grep infisical
```

Expected output:
```
[infisical] Fetching secrets from Infisical workspace...
[infisical] Secrets loaded successfully
```

Check that secrets are available inside the container:

```bash
# Hermes container
docker compose exec company-ops bash -c 'echo $DISCORD_BOT_TOKEN'

# Engineering container
docker compose exec engineering bash -c 'echo $ANTHROPIC_API_KEY'
```

---

## Phase 5: Local Development Without Infisical (Optional)

For local development, you can optionally populate `.env` manually (never commit credentials):

1. Copy `.env.example` to `.env`
2. Fill in real values (locally only, do not commit)
3. Omit `INFISICAL_TOKEN`
4. Entrypoint will use the `.env` file instead

**Note:** This is less secure than Infisical. Always use Infisical for production and shared environments.

---

## Troubleshooting

### Container starts but secrets not injected

**Check 1:** Verify `INFISICAL_TOKEN` is set:
```bash
docker compose exec company-ops bash -c 'echo $INFISICAL_TOKEN'
```

**Check 2:** Verify token has correct permissions:
- Log into Infisical
- Go to Machine Identities → your token → check folder permissions
- Ensure the folder (`/secrets/hermes/` or `/secrets/infra/`) is readable

**Check 3:** Check infisical CLI is installed in image:
```bash
docker compose exec company-ops infisical --version
```

### "Failed to fetch secrets from Infisical"

**Check 1:** Token is valid and not expired:
- Log into Infisical → Machine Identities → Verify token is active

**Check 2:** Token has network access to Infisical:
```bash
docker compose exec company-ops bash -c 'curl -s https://app.infisical.com/health'
```

**Check 3:** Token has correct project/folder access:
- Hermes token must have access to `buildmy-house-ops` project, `/secrets/hermes/` folder
- Engineering token must have access to `buildmy-house-infra` project, `/secrets/infra/` folder

### Container runs but app fails with missing secrets

**Check:** Verify secret names match expected environment variable names:
- Infisical path `/secrets/hermes/discord_bot_token` → env var `DISCORD_BOT_TOKEN` (uppercase, underscores)
- Infisical path `/secrets/infra/r2_access_key_id` → env var `R2_ACCESS_KEY_ID`

To verify what was actually injected:
```bash
docker compose exec company-ops bash -c 'env | grep -E "(DISCORD|POSTGRES|R2|ANTHROPIC)" | sort'
```

---

## Security Notes

1. **Never commit tokens to git** — INFISICAL_TOKEN must be set at runtime (environment variable, CI/CD secret, or secure vault)
2. **Rotate tokens regularly** — Infisical supports token expiration; set a schedule for rotation
3. **Use separate tokens per environment** — Create different machine identities for prod, staging, dev
4. **Audit access** — Infisical logs all secret access; monitor for unexpected reads
5. **Two-tier architecture** — Hermes cannot read `/secrets/infra/` (GitHub, Anthropic, R2 keys remain secret from Hermes)

---

## Next Steps

1. Create Infisical account and organization
2. Create two projects (`buildmy-house-ops`, `buildmy-house-infra`)
3. Create machine identities and generate tokens
4. Populate secrets in Infisical following Phase 3
5. Test locally: set `INFISICAL_TOKEN` and start container
6. Verify secrets are injected (see Phase 4.3)
7. For production, configure CI/CD to provide `INFISICAL_TOKEN` (GitHub Actions secrets, etc.)

---

## Reference: Full Secret Path Mapping

### buildmy-house-ops → `/secrets/hermes/`

```
/secrets/hermes/discord_bot_token           → DISCORD_BOT_TOKEN
/secrets/hermes/discord_allowed_users       → DISCORD_ALLOWED_USERS
/secrets/hermes/discord_allowed_channels    → DISCORD_ALLOWED_CHANNELS
/secrets/hermes/discord_announce_channel    → DISCORD_ANNOUNCE_CHANNEL
/secrets/hermes/discord_hil_channel         → DISCORD_HIL_CHANNEL
/secrets/hermes/discord_finance_channel     → DISCORD_FINANCE_CHANNEL
/secrets/hermes/discord_dm_user             → DISCORD_DM_USER
/secrets/hermes/discord_daily_prompt        → DISCORD_DAILY_PROMPT
/secrets/hermes/discord_investor_prompt     → DISCORD_INVESTOR_PROMPT
/secrets/hermes/discord_questions_prompt    → DISCORD_QUESTIONS_PROMPT
/secrets/hermes/nous_api_key                → NOUS_API_KEY
/secrets/hermes/postgres_password           → POSTGRES_PASSWORD
/secrets/hermes/company_password            → COMPANY_PASSWORD
/secrets/hermes/observer_password           → OBSERVER_PASSWORD
/secrets/hermes/analytics_password          → ANALYTICS_PASSWORD
```

### buildmy-house-infra → `/secrets/infra/`

```
/secrets/infra/github_token                 → GITHUB_TOKEN
/secrets/infra/r2_endpoint                  → R2_ENDPOINT
/secrets/infra/r2_access_key_id             → R2_ACCESS_KEY_ID
/secrets/infra/r2_secret_access_key         → R2_SECRET_ACCESS_KEY
/secrets/infra/slack_webhook_url            → SLACK_WEBHOOK_URL
/secrets/infra/anthropic_api_key            → ANTHROPIC_API_KEY
/secrets/infra/opencode_zen_api_key         → OPENCODE_ZEN_API_KEY
/secrets/infra/tokenrouter_api_key          → TOKENROUTER_API_KEY
/secrets/infra/axiom_token                  → AXIOM_TOKEN
/secrets/infra/axiom_org_id                 → AXIOM_ORG_ID
```

---

## Infisical Documentation

For additional help, see:
- [Infisical Platform Docs](https://infisical.com/docs)
- [Machine Identities Guide](https://infisical.com/docs/guides/machine-identities)
- [Secret References & Hierarchies](https://infisical.com/docs/platform/secret-reference)
- [API Token Authentication](https://infisical.com/docs/api-reference/authentication/machine-identities)

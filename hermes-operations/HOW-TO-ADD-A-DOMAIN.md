# How to Add a Domain

Hermes can add new domains from inside the running container. No rebuild
needed — commit and push, the container pulls on restart.

## Steps (from inside the container)

### 1. Ensure repo is current

```bash
cd /opt/hermes-operations
git pull --ff-only
```

### 2. Create the domain directory

```bash
mkdir -p domains/<domain-name>/{knowledge,.opencode/agents}
```

### 3. Create domain files

Copy an existing domain as template:

```bash
cp -r domains/engineering/ domains/<domain-name>/
```

Then customize these files:

#### `domains/<domain-name>/AGENTS.md`
- Domain-specific agent rules
- Extends `shared/AGENTS.base.md`
- Defines domain-specific tools, workflows, quality standards

#### `domains/<domain-name>/AGENTS_STEWARD.md`
- Coordination protocol for this domain
- Layout and ownership rules (which files does this domain own?)
- Frozen contracts (if any)
- DoD checklists for domain-specific work
- Steward scope path: `house_designer/<domain-name>`

#### `domains/<domain-name>/PLAN.md`
- Claim board template
- Track legend for this domain
- Knowledge base index

#### `domains/<domain-name>/opencode.json`
- Provider config (usually same as shared base)
- MCP config (Steward connection)
- Instructions array: `["../shared/AGENTS.base.md", "AGENTS.md", "AGENTS_STEWARD.md"]`

#### `domains/<domain-name>/.opencode/agents/`
- Manager agent definition (e.g., `<domain>-manager.md`)
- Worker agent template

### 4. Register in Steward

The domain's Steward scope is `house_designer/<domain-name>`. This is
automatic — Steward scopes by path prefix.

Save a memory with the domain's key facts:

```bash
steward save_memory \
  --title "<Domain> domain added to hermes-operations" \
  --content "Domain: <domain-name>. Scope: house_designer/<domain-name>. ..." \
  --scope_path "house_designer/<domain-name>" \
  --kind "decision"
```

### 5. Test locally

```bash
cd domains/<domain-name>
cat opencode.json | python3 -m json.tool
```

### 6. Commit and push

```bash
cd /opt/hermes-operations
git add domains/<domain-name>/
git commit -m "Add <domain-name> domain"
git push
```

### 7. Pull into other containers

Other running containers will pull the new domain on next restart.
For immediate use in the current container:

```bash
cd /opt/hermes-operations && git pull
```

## Domain Structure

```
hermes-operations/
  shared/                       # Common rules, graphify, scripts
    AGENTS.base.md              # Universal rules (all domains extend this)
    opencode.base.json          # Base config template
    MODEL_POLICY.md
    .opencode/                  # Shared graphify skill/plugin
    scripts/
  domains/
    engineering/                # Software development
      AGENTS.md                 # Extends shared + engineering rules
      AGENTS_STEWARD.md         # Engineering coordination
      PLAN.md                   # Engineering claim board
      opencode.json             # Engineering OpenCode config
      .opencode/agents/         # Engineering manager + worker
      knowledge/                # Accumulated engineering knowledge
    marketing/                  # Content, SEO, campaigns
      ...
    design/                     # UI/UX, brand, accessibility
      ...
    <new-domain>/               # Your new domain here
      AGENTS.md
      AGENTS_STEWARD.md
      PLAN.md
      opencode.json
      .opencode/agents/
      knowledge/
```

## Key Principles

1. **Shared base is law**: All domains extend `shared/AGENTS.base.md`.
   Never duplicate universal rules in domain files.
2. **Domain-specific is local**: Only put domain-specific rules in the
   domain's AGENTS.md. If it applies to all domains, put it in shared.
3. **Knowledge accumulates**: Each domain has a `knowledge/` directory.
   Files added here are tracked on GitHub and pulled by containers.
4. **Steward scope is automatic**: The scope path `house_designer/<domain>`
   handles access control. No manual Steward config needed.
5. **Runtime clone, not build-time**: The Docker entrypoint clones the
   repo on first start, pulls on subsequent starts. No rebuild needed
   to add domains.

## Adding a New Track to an Existing Domain

Edit the domain's `PLAN.md`:

1. Add the track to the Track Legend table
2. Add tickets to the Claim Board with the new track label
3. Commit and push

No other changes needed — the existing manager and worker will handle it.

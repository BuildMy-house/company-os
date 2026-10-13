# Context compaction at 100k tokens (OpenCode + Hermes)

Decided 2026-10-13 (Steward task `memory-compaction-overhaul-100k-compaction-hermes-opencode-c`).
Goal: every agent compacts its context at about 100,000 tokens instead of near the model's full window.

## Hermes (`hermes/config.yaml`)

```yaml
compression:
  enabled: true
  threshold_tokens: 100000   # absolute cap; trigger = LOWER of ratio vs this cap
```

`compression.threshold` (ratio of the active model's window, default 0.50) is left alone. Windows: primary
`meituan/longcat-2.0:free` ~1.05M, fallback `glm-5.3-flash` 1M, so the ratio alone would compact at ~500k.
`threshold_tokens` is Hermes's own absolute cap (see `hermes_cli/config.py` `_show_compression_section`:
"takes lower of ratio vs absolute"; `agent/agent_init.py` reads `compression.threshold_tokens`, clamps it to the
window, and Hermes floors the effective threshold at 64K). The summariser is `auxiliary.compression`
(tokenrouter `z-ai/glm-5.3-flash`, 1M window), so 100k fits. `memory.nudge_interval` was lowered 10 -> 5.
Takes effect only after the Hermes image is rebuilt and the gateway upgraded (human-gated).

## OpenCode (every `opencode.json` in this repo)

OpenCode has no absolute-threshold option. Source behaviour (opencode 1.19.0, `overflow` check): compaction fires when
`tokens.total >= usable`, where
`usable = limit.input ? limit.input - compaction.reserved : limit.context - min(limit.output, 32000)`, and
`reserved` defaults to `min(20000, output)`. So `compaction.reserved` only matters when the model has `limit.input`.
We therefore set, per model actually used, `limit.input = 120000` and `compaction.reserved = 20000`
=> trigger at **100,000 tokens**. `prune: true` additionally prunes old tool outputs.

Two further facts that drove this:
- A custom-provider model (e.g. `tokenrouter/z-ai/glm-5.3-flash`) with no `limit` gets `limit.context = 0`, and
  OpenCode then **never auto-compacts** it. Giving it a `limit` fixes that.
- `limit.context`/`limit.output` values: `mimo-v2.6-flash-free` 200000/32000 and `glm-5.3-flash` 1000000/131072 are
  from `opencode models --verbose`; `mimo-v2.5-free` (200000/32000) is an **assumption** because that id is no longer
  in the local (1.19.0) model catalog. Only `limit.input` (120000) is load-bearing for the 100k trigger.
- 100k fits every model used here (smallest window 200k). A model added later without a `limit` block will not compact
  at 100k (built-in models: `context - 32000`; custom models: never) -- add one.

---
steward_task: manage-memory-compaction-overhaul-impl-opencode-hermes-compa
date: 2026-10-13
---
# OpenCode has no absolute compaction threshold; trigger = limit.input - compaction.reserved

OpenCode compacts when total tokens >= (limit.input - compaction.reserved) if the model has limit.input, else (limit.context - min(limit.output, 32000)); `reserved` is ignored without limit.input. A custom-provider model with no `limit` block has context 0 and never auto-compacts. To compact at 100k set limit.input=120000 and reserved=20000 on each model used. Hermes instead has an absolute cap: compression.threshold_tokens (lower of ratio vs cap). Details: docs/COMPACTION.md.

# Model policy

Hermes (CEO) and the engineering manager use this same policy. The model name,
quota status, reason, estimated cost, and actual provider charge belong in every
delegation result.

## Default

Hermes uses Nous Research Labs `tencent/hy3:free` for CEO conversations. This
deployment authenticates with `NOUS_API_KEY` at
`https://inference-api.nousresearch.com/v1`.

Use OpenCode Zen `opencode/mimo-v2.5-free` for planning, summaries, content, SEO,
low-risk code, and test scaffolding when the account has free quota. The
model is served by OpenCode Zen, not TokenRouter. “Free” means an available Zen
allocation and must be checked at runtime.

Use `tokenrouter/z-ai/glm-5.3-flash` only when the free route is unavailable or
the task needs stronger reasoning. Current listed rates are $0.075 per million
input tokens and $0.250 per million output tokens. Unknown models are treated
as expensive until their price and quota are recorded.

## Learning rule

After every delegation, record the selected model, quota state, token counts if
available, estimated cost, actual cost if available, outcome, and justification.
If a paid model was used for a task that the free model could have handled,
record a routing lesson for that task type. Future routing must avoid that
model for that task type until the lesson is explicitly reviewed. A paid model
is acceptable for security, deployment, financial, policy, or genuinely complex
engineering work when the manager records why.

The policy optimizes for successful useful work, not lowest token price: a cheap
failed attempt followed by an expensive retry is worse than one justified
escalation.

Pricing reference: https://www.tokenrouter.com/models/
Flash reference: https://www.tokenrouter.com/models/z-ai/glm-5.3-flash/

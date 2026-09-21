# Hive Architecture Guide

The canonical workspace guide is
[`../../.agents/hive-architecture.md`](../../.agents/hive-architecture.md).

Company OS implements the first slice: an Elixir/OTP coordination service with
durable Postgres work/events/leases, `LISTEN/NOTIFY` or PubSub wake-ups,
supervised consumers, and task-ID-based completion/failure signals. Use
Postgrex and plain SQL initially; Ecto is optional. Hermes remains on its
current Nous configuration; worker model selection belongs to the engineering
manager, which should prefer available free OpenCode models such as MiMo or
GLM Flash. Elixir is the default for new Company OS infrastructure, while
Python remains appropriate for existing integrations, agent tooling, and data
work where it is the simpler choice.

Do not introduce a second runtime, a token currency, or a full auction market
until the event/lease path is reliable and measured.

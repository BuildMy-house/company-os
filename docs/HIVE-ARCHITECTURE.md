# Hive Architecture Guide

The canonical workspace guide is
[`../../.agents/hive-architecture.md`](../../.agents/hive-architecture.md).

Company OS implements the first slice: a Python coordination service with
durable Postgres work/events/leases, `LISTEN/NOTIFY` wake-ups, idempotent
consumers, and task-ID-based completion/failure signals. Hermes remains on its
current Nous configuration; worker model selection belongs to the engineering
manager, which should prefer available free OpenCode models such as MiMo or
GLM Flash.

Do not introduce a second runtime, a token currency, or a full auction market
until the event/lease path is reliable and measured.

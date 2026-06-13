# Overview

`quant-execution-engine` is the platform's **Execution engine** — the single canonical order router
and the sole owner of broker order-routing credentials. Strategies submit one normalized order to the
gateway (`/api/v2/engines/execution/*`); the engine validates it (kill-switch → idempotency dedupe →
capability gate → PTRM risk gate → price-band → stage ladder), persists the lifecycle to the durable
`execution.*` store, routes it to a `BrokerAdapter` (`SimAdapter`, `LiberatorAdapter`,
`SettradeAdapter`), and streams normalized order updates back out. `live` is gated; `sim` is the
default. No strategy, the gateway, or any host ever holds a broker credential.

Start at the **documentation hub** — [`README.md`](README.md) — which links the architecture, API,
operations, and data-model references. The canonical build history is
[`plans/ROADMAP.md`](plans/ROADMAP.md); the agent guide is [`../CLAUDE.md`](../CLAUDE.md).

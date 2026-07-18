# quant-execution-engine — Documentation

The **Execution Engine** is the platform's single **canonical order router** and the **sole owner of
broker order-routing credentials**. It is a FastAPI service (container `:8000`, host `:8400`) on the
external `quant-network`, proxied by `quant-api-gateway` under `/api/v2/engines/execution/*`. It
writes the durable `execution.*` store in `quant-infra-db` (Postgres, `db_execution`) and ships its
own Redis sidecar (dedupe / single-flight lock / rate-limit). Strategies submit one `NormalizedOrder`
and **never speak a broker API**.

> **State (2026-06-13):** Phases 0–6 complete (durable store, engine core + `SimAdapter`,
> `LiberatorAdapter` + `StreamingProAdapter` real venues, order-update stream + order book,
> safety/ops hardening); **Phase 7 (this documentation) in progress**. `live` is **gated** — no
> real-money default. See [`plans/ROADMAP.md`](plans/ROADMAP.md).
>
> **Updated 2026-07-18:** broker-023 / `settrade_v2` (the Settrade Open API) was removed — real
> brokers are now Sim + Liberator + Streaming Pro (the self-built `settrade-streaming-api` bridge).

This page is the **documentation hub** — start here and follow the links.

## Architecture

| Doc | What it covers |
|-----|----------------|
| [`architecture/overview.md`](architecture/overview.md) | Service topology, the two planes (D1), gateway-proxy position, sole-credential-owner invariant, the safety ladder, kill-switch, public mode |
| [`architecture/state-machine.md`](architecture/state-machine.md) | The frozen 9-state / 13-edge order machine, terminal vs in-flight states, append-only audit, idempotency, the reconciliation window |
| [`architecture/adapters.md`](architecture/adapters.md) | The `BrokerAdapter` interface, the full capability matrix, the two structural consequences, Sim / Liberator / Streaming Pro adapter notes |
| [`architecture/security-boundary.md`](architecture/security-boundary.md) | Credential ownership, public vs owner mode, the PTRM gate + price-band, the kill-switch, logging redaction |

## API reference

| Doc | Endpoint |
|-----|----------|
| [`api/health.md`](api/health.md) | `GET /health` — readiness (DB / Redis / brokers / order book) |
| [`api/orders-submit.md`](api/orders-submit.md) | `POST /orders` — submit a `NormalizedOrder` (owner mode) |
| [`api/orders-get.md`](api/orders-get.md) | `GET /orders/{client_order_id}` — order status |
| [`api/orders-cancel.md`](api/orders-cancel.md) | `DELETE /orders/{client_order_id}` — cancel (owner mode; not kill-switch-gated) |
| [`api/orders-amend.md`](api/orders-amend.md) | `PATCH /orders/{client_order_id}` — native or cancel+replace amend (owner mode) |
| [`api/orders-stream.md`](api/orders-stream.md) | `GET /orders/stream` — SSE order-update stream (`Last-Event-ID` replay, filters) |
| [`api/capabilities.md`](api/capabilities.md) | `GET /capabilities` — the capability matrix + broker runtime health |
| [`api/order-book.md`](api/order-book.md) | `GET /order-book/{symbol}[/stream]` — read-only L2 book (default-off) |
| [`api/admin.md`](api/admin.md) | `/admin/kill-switch*` + `/admin/orders/{cid}/audit` + `/admin/audit/export` (owner mode) |

All endpoints are gateway-proxied under `/api/v2/engines/execution/*` (SSE streams pass through
unbuffered). Order-submission + `/admin/*` are **owner-mode only**; health, capabilities, reads, and
streams are public-readable (api-key-gated).

## Operations

| Doc | What it covers |
|-----|----------------|
| [`operations/bring-up.md`](operations/bring-up.md) | Bring-up order, the compose configs (public / +Liberator / +Streaming Pro overlay), the schema prerequisite, health checks, tear-down |
| [`operations/configuration.md`](operations/configuration.md) | Every `EXECUTION_ENGINE_*` env var — name / type / default / effect / SecretStr |
| [`operations/kill-switch.md`](operations/kill-switch.md) | Engage / disengage procedures, the stage-flip rule, the breaker relationship |
| [`operations/troubleshooting.md`](operations/troubleshooting.md) | Common failure modes: breaker tripped, stuck pendings, duplicate-burst, DB/Redis down, gateway 5xx |
| [`operations/liberator-session-self-heal.md`](operations/liberator-session-self-heal.md) | The bundled Liberator auto-relogin monitor (enabled): the self-heal loop, the iPhone-OTP dependency, the fail-loud alert + response, config surface, enable/disable, the two live gotchas |

## Data model

| Doc | What it covers |
|-----|----------------|
| [`data/execution-schema.md`](data/execution-schema.md) | `db_execution` — `orders` / `fills` / `order_events` columns, constraints, triggers, indexes, grants |
| [`data/state-machine-transitions.md`](data/state-machine-transitions.md) | The verified 13-edge legal-transition table |

## Plans (build history)

The phase-by-phase build lives under [`plans/`](plans/) — [`plans/ROADMAP.md`](plans/ROADMAP.md) is
canonical. Phase 7 (this docs work) is [`plans/phase7-documentation.md`](plans/phase7-documentation.md).

## Conventions used throughout these docs

- **Money is `Decimal`, serialised as a string on the wire** (e.g. `"35.50"`), never `float`. Prices
  are `numeric(18,6)` in the DB.
- **Timestamps are UTC** (e.g. `"2026-06-13T09:00:00Z"`); display in `Asia/Bangkok` at the edge.
- **Secrets are placeholders.** Broker creds, PINs, and API keys never appear with real values;
  `SecretStr` examples use `<your-value-here>`.
- **In-container hostnames** (`quant-execution-engine`, `quant-postgres`, `quant-execution-redis`) are
  used inside `quant-network`; the host port `:8400` is for developer access only.
- **`live` is gated.** Examples route to `SimAdapter` unless explicitly noted; no example places a
  real-money order.

## Cross-repo references

| Resource | Path |
|----------|------|
| Engine agent guide | [`../CLAUDE.md`](../CLAUDE.md) |
| Umbrella system map + engine catalog | [`../../CLAUDE.md`](../../CLAUDE.md) |
| Umbrella feature roadmap | [`../../plans/feature-execution-engine/ROADMAP.md`](../../plans/feature-execution-engine/ROADMAP.md) |
| ADR (D1–D13, frozen contracts) | [`../../.claude/knowledge/feature-execution-engine.md`](../../.claude/knowledge/feature-execution-engine.md) |
| Broker research (cited) | [`../.claude/knowledge/broker-research-liberator.md`](../.claude/knowledge/broker-research-liberator.md) (the Settrade research note was removed with broker-023 on 2026-07-18) |
| Capability matrix / contract / state machine | [`../.claude/knowledge/capability-matrix.md`](../.claude/knowledge/capability-matrix.md), [`../.claude/knowledge/normalized-order-contract.md`](../.claude/knowledge/normalized-order-contract.md), [`../.claude/knowledge/order-state-machine.md`](../.claude/knowledge/order-state-machine.md) |
| Decision log | [`../.claude/knowledge/decision-log.md`](../.claude/knowledge/decision-log.md) |
| Order-update stream / order book | [`../.claude/knowledge/order-update-stream.md`](../.claude/knowledge/order-update-stream.md), [`../.claude/knowledge/order-book-service.md`](../.claude/knowledge/order-book-service.md) |
| Order-routing safety playbook | [`../.claude/playbooks/order-routing-safety.md`](../.claude/playbooks/order-routing-safety.md) |
| Umbrella operator runbook | [`../../.claude/playbooks/execution-engine-runbook.md`](../../.claude/playbooks/execution-engine-runbook.md) |
| Canonical schema (SQL) | `quant-infra-db/init-scripts/12_schema_execution.sql`, `13_execution_strategy_id.sql` |

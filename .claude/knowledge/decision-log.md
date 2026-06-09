# Decision log — quant-execution-engine

> The cross-cutting Decision Log **D1–D13** is authored in the umbrella roadmap
> ([`plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md))
> and gated by the ADR
> ([`.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md)).
> Phase 0 confirms each; this file records the per-service decisions + the findings the
> broker research resolved.

## Accepted (D1–D13, summary)

| # | Decision |
|---|---|
| D1 | Execution and streaming are **separate planes/services**; this is execution-only. |
| D2 | **Standalone `quant-execution-engine`** (host `:8400`), gateway-proxied — not a gateway module. |
| D3 | **`BrokerAdapter` interface** (`place/cancel/amend/get_open_orders/get_positions/get_account/capabilities`). |
| D4 | **`NormalizedOrder` contract** + status enum `NEW\|PARTIALLY_FILLED\|FILLED\|CANCELLED\|REJECTED\|EXPIRED`. |
| D5 | **Client-generated `client_order_id`** idempotency key; dedupe before routing. |
| D6 | **Durable order store in `quant-infra-db`** — `execution.orders` + `fills` + append-only `order_events`. |
| D7 | **Per-adapter capability matrix**; router rejects unsupported combos up front. |
| D8 | **Reconciliation loop** repairs broker-truth ↔ local-state drift. |
| D9 | **`LiberatorAdapter` wraps the existing `liberator-trading-api`** over HTTP; does not re-implement it. |
| D10 | **Auth/session stays inside each adapter** (Liberator OTP vs Settrade OAuth). |
| D11 | **`SimAdapter` + global kill-switch + pre-trade risk gate from day one.** |
| D12 | **Normalized order-update stream out** (WS/events). |
| D13 | **Strategies submit behind a flag** (`*_EXECUTION_MODE = off\|sim\|live`, default off/sim). |

## Per-service decisions (this repo)

| # | Decision | Rationale |
|---|---|---|
| E1 | **Env prefix `EXECUTION_ENGINE_*`**, `pydantic-settings`. | Matches the umbrella per-service convention; no secret in repo. |
| E2 | **`EXECUTION_ENGINE_STAGE = sim\|paper\|micro_live\|live`, default `sim`.** | The execution analogue of the tfex capital ladder + marketdata `mirror\|engine` cutover — gate the dangerous rung, default the safe one. |
| E3 | **Public mode disables order-submission endpoints** (Docker default `true`). | Mirrors the marketdata-engine public/owner split; submits are owner-mode only. |
| E4 | **Own Redis sidecar** (`quant-execution-redis`), internal-only. | Dedupe / single-flight submit lock / rate-limit, distinct from the gateway's Redis (D2 isolation). |
| E5 | **`db_execution` Postgres database** for the `execution.*` schema. | Per-engine DB boundary, like `db_market_data`. |
| E6 | **Scaffold ships `/health` only**, ≥90% coverage gate from day one. | Safe skeleton; no order path can run until Phase 2 wires the state machine + risk gate. |

## Findings resolved by the broker research (2026-06-09)

| # | Finding | Consequence |
|---|---|---|
| R1 | **Neither Liberator nor Settrade accepts a client idempotency key** (broker order_no only). | The engine owns the `client_order_id ↔ broker_order_id` mapping; dedupe + reconcile is how we approximate exactly-once (D5/D8). Pin "at-least-once + dedupe", not "exactly-once", in Phase 0. |
| R2 | **Liberator has no amend route; Settrade has native `change_order`.** | `BrokerAdapter.amend` is uniform; `LiberatorAdapter.amend` = cancel+replace (declared non-atomic). Capability divergence enforced by the router (D7). |
| R3 | **Settrade exposes a native order-update push** (`subscribe_derivatives_order`); Liberator does not. | Phase-5 stream: Settrade feeds it directly; Liberator state is reconciled/poll-normalized into the same shape (D12). |
| R4 | **Settrade `price_type`/`validity_type`/`trigger_session` enum sets are SDK-passthrough strings.** | The exact venue enums are pinned in Phase 4 against the live venue, not guessed in the contract — `(confirm P4)` cells in the capability matrix. |
| R5 | **Both brokers take a per-order PIN; auth differs** (Liberator OTP/SMS + Redis token, Settrade OAuth auto-refresh + rate-limit). | Session liveness is adapter-local (D10); the health/reconcile path must detect a dead session before it silently drops orders. |

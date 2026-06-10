# Decision log — quant-execution-engine

> The cross-cutting Decision Log **D1–D13** is authored in the umbrella roadmap
> ([`plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md))
> and pinned by the ADR
> ([`.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md)).
> **Phase 0 confirmed all thirteen as drafted — ACCEPTED 2026-06-10; the ADR is the source of
> truth.** This file records the summary, the per-service decisions, the broker-research
> findings, and the Phase 0 pinned resolutions.

## Accepted (D1–D13, summary — confirmed in the Phase 0 ADR, 2026-06-10)

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

## Phase 0 pinned resolutions (2026-06-10)

The ROADMAP's "Open questions / risks" were resolved as written decisions in the ADR
(Pinned **§A–§G**); owner stances + parameters confirmed 2026-06-10. One-line summaries —
the ADR text governs:

| § | Resolution |
|---|---|
| §A | Delivery guarantee = **at-least-once + dedupe + reconcile + idempotent re-submit, NOT exactly-once** (R1). `client_order_id` standard: **UUIDv4**, client-generated, opaque to the engine (time-ordered drop-ins acceptable; the id is never parsed for time). |
| §B | `client_order_id ↔ broker_order_id` persisted **atomically with `PENDING_NEW → NEW`**; lost-ack fallback: fuzzy match `(account, symbol, side, qty)` within **±5 s** of the persisted submit ts; a stuck `PENDING_NEW` resolves bounded, never blocks routing indefinitely. |
| §C | `NormalizedOrder` / `NormalizedOrderResult` / `NormalizedStatus` **frozen** — `Decimal`-as-string wire, `int` qty, UTC. |
| §D | `BrokerAdapter` 7-method interface frozen; amend semantics declared per adapter. |
| §E | Order state machine frozen — 9 states + complete legal-transition table. |
| §F | Capability-matrix shape frozen — per-`(broker, market)` sets, router-enforced pre-venue; Liberator amend = cancel+replace **non-atomic** (queue-loss declared); `(confirm P4)` enums deferred-by-design (R4). |
| §G | Order-type validation = distinct pre-flight class per `(broker, market, order_type)` (Phase 3/4); auth liveness = **~30 s heartbeat + circuit breaker** per adapter; blast radius = PTRM caps + kill-switch **(reject new + mass-cancel open)** as Phase-2 milestones; streaming = external read-only dependency (D1 reaffirmed). |

## Phase 2 realisation decisions (E7–E12, 2026-06-10)

| # | Decision | Rationale |
|---|---|---|
| E7 | **Synchronous in-request sim fills**; `repositories.apply_fill()` is the standalone seam Phase-3/4 stream/reconcile workers reuse. | Deterministic acceptance from one POST; no background ordering nondeterminism for an in-proc adapter. |
| E8 | **Additive `engine_state` Result field** (internal 9-state truth) beside the frozen 6-value `status`. | Keeps the frozen enum intact while keeping the §B reconciliation window operator-visible. Contract addendum, not a change. |
| E9 | **Kill-switch precedes even dedupe** in the submit path; cancels are NOT kill-switch-blocked (mass-cancel uses the cancel path). | Hard rule 3 ("checked first") wins over the validation-list ordering; cancels reduce risk. |
| E10 | **Runtime kill-switch trip = Redis key + admin endpoints** (engine-direct, owner-mode, never proxied); env flag is the boot-time backstop and pins over runtime disengage. | An env-only switch needs a restart, during which nothing mass-cancels. |
| E11 | **Single-flight submit lock is politeness; the orders PK is correctness.** Lock-miss ⇒ brief store-poll ⇒ 200 duplicate or 409 `submit_in_flight`; Redis-down ⇒ PK arbitrates. PTRM rate/burst fail-open in `sim|paper`, fail-closed in `micro_live|live`. | At-least-once + dedupe (§A) holds with or without Redis. |
| E12 | **`metadata` is the sim control channel** (`sim_fills`, `sim_reject`) — never venue-sent by any adapter, never persisted. `reject_reason` persists durably (Phase-2 column); the audit trigger runs with INVOKER rights so the service role holds INSERT on `order_events` (append-only stays trigger-enforced). | Deterministic lifecycle steering without contract surface; real-money audit must not live in a cache. |

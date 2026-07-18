# Architecture — Overview

The Execution Engine is a standalone `EXTERNAL` engine: **one service routes every order and holds
every broker credential**; strategies submit one `NormalizedOrder` and never speak a broker API.
This page is the system topology. For the order lifecycle see [`state-machine.md`](state-machine.md),
for the broker abstraction see [`adapters.md`](adapters.md), for the trust boundary see
[`security-boundary.md`](security-boundary.md).

## Topology

```
   strategies (csm-set, tfex-…)            quant-openbb
        │  POST normalized orders                │
        │  + react to the order-update stream    │
        ▼                                        ▼
   ┌─────────────────────────────────────────────────────┐
   │  quant-api-gateway   (FastAPI :8000)                 │  thin reverse proxy
   │  /api/v2/engines/execution/*    ·    holds NO cred   │
   └───────────────────────────┬─────────────────────────┘
                               │  proxy → :8400
                               ▼
   ┌─────────────────────────────────────────────────────┐
   │  quant-execution-engine   (host :8400 / container    │  EXTERNAL engine
   │  :8000)  submit pipeline · durable store · reconcile │  sole broker-credential owner
   │  own Redis sidecar (dedupe / single-flight / rate)   │
   └───┬──────────────────┬────────────────────┬──────────┘
       │                  │                     │
       ▼                  ▼                     ▼
   SimAdapter      LiberatorAdapter        StreamingProAdapter
  (in-process)        │ HTTP                   │ HTTP
                      ▼                        ▼
            liberator-trading-api        settrade-streaming-api bridge
            (internal :8200, bundled)    (host :8700, bundled — SET + TFEX)
                               │
            writes (durable)   ▼
   quant-infra-db (Postgres · db_execution)
     execution.orders        idempotent PK (client_order_id), 9-state CHECK enum
     execution.fills         UNIQUE (client_order_id, broker_fill_id)
     execution.order_events  append-only audit (one row per transition)
```

`liberator-trading-api` is **internal-only** — no host port, bundled into owner mode via a compose
overlay, never a peer service on `quant-network`. `SimAdapter` is in-process; the Streaming Pro
`settrade-streaming-api` bridge is bundled via `docker-compose.streaming.yml`. (The Settrade Open API
— broker-023 / `settrade_v2` — was removed on 2026-07-18.) Use **service hostnames inside
containers** (`quant-execution-engine`, `quant-postgres`); the host port `:8400` is for developer
access only.

## The two planes (D1)

This service is the **execution / order-command plane** only — low volume, real money, idempotent,
durable. The **market-data / streaming plane** (ticks, OHLCV, order-book persistence) is a separate
concern owned by `quant-marketdata-engine` + `order-book-infrastructure`.

| | Execution plane (this service) | Market-data plane (separate) |
|---|---|---|
| Volume | Low (tens/day) | High (ticks) |
| A lost message means | **a real loss** — a duplicated order is money | a resubscribe — a dropped tick is re-fetched |
| Guarantee | at-least-once + dedupe + durable state + reconcile | best-effort streaming |
| Storage | durable `execution.*` (source of truth) | TimescaleDB OHLCV / no durable book |

The engine **may read** a market-data feed for the advisory price-band check and for `SimAdapter`
fill pricing, and it runs an in-engine **read-only** order-book service (default-off) — but it
**never owns** market-data infrastructure and never persists a tick. A duplicated order is a real
loss; a dropped tick is a resubscribe. That asymmetry is why the planes stay separate; see
[`../api/order-book.md`](../api/order-book.md) for the sanctioned read-only carve-out.

## The gateway-proxy position

Consumers (strategies, OpenBB) call the gateway's `/api/v2/engines/execution/*`; the gateway is a
**thin reverse proxy that holds no broker credential**. It forwards the caller's `X-API-Key` and the
`X-Strategy-Id` header (gateway PR #24 stopped stripping it), and passes SSE streams through
unbuffered. Upstream failures map to clean status codes; typed rejection envelopes pass through
verbatim.

## The sole-credential-owner invariant

Only this service holds broker order-routing credentials:

- **Liberator** — `LIBERATOR_API_KEY` + per-order `LIBERATOR_PIN` (OTP/2FA-derived session).
- **Streaming Pro** — only the bridge api-key (`STREAMING_PRO_API_KEY`); the bundled
  `settrade-streaming-api` bridge owns USERNAME/PASSWORD/PIN and stamps the PIN itself, so the engine
  holds **no PIN**.

They live **only** in this service's gitignored `.env` — never committed, never logged, never held
by a strategy, the gateway, or a host. See [`security-boundary.md`](security-boundary.md).

## The safety ladder — `EXECUTION_ENGINE_STAGE`

The single most important operational control. Default `sim`; `live` is gated.

| Stage | Default | Routes a placement to | Notes |
|-------|:---:|---|---|
| `sim` | ✅ | `SimAdapter` (in-process, deterministic) | No broker session needed; bit-for-bit reproducible |
| `paper` | | `SimAdapter` | Each configured broker session is kept **live for reads**, but every placement is **intercepted to sim** |
| `micro_live` | | the **real** venue (`broker=liberator`/`streaming_pro`) at **PTRM-capped** size | Requires owner mode + kill-switch disengaged + the broker runtime configured |
| `live` | | — | **Gated**: rejected with a typed `stage_rejected` (403). No real-money default |

**No order reaches a real broker below `micro_live`, and never without owner mode on, the
kill-switch disengaged, and the broker runtime configured.** Flipping the stage is an operator
action governed by the kill-switch-first rule — see [`../operations/kill-switch.md`](../operations/kill-switch.md).

## The kill-switch

`EXECUTION_ENGINE_KILL_SWITCH_ENGAGED` (boot backstop) + the runtime admin trip
(`POST /admin/kill-switch/engage`) **override every stage** and are checked **first** in the submit
path — and in the **amend** path, which (unlike the un-gated cancel path) is gated because an amend
can *increase* exposure. Engaging trips a **mass-cancel** of every open order (flatten-and-halt).
See [`../api/admin.md`](../api/admin.md) and [`../operations/kill-switch.md`](../operations/kill-switch.md).

## Public mode

`EXECUTION_ENGINE_PUBLIC_MODE=true` (the **Docker default**) disables all order-submission and admin
endpoints (`POST`/`DELETE`/`PATCH /orders`, `/admin/*`) with a typed `public_mode` (403). Health,
`/capabilities`, order reads, and the SSE streams stay available (api-key-gated). Owner mode
(`PUBLIC_MODE=false`) opens the full surface and is the only mode that holds broker credentials.

## Design principles (from the ADR, D1–D13)

1. **Two planes, never merged.** Execution is durable/idempotent; market data is best-effort (D1).
2. **One router owns every credential.** Add a broker = write one adapter, touch no strategy (D7/D9).
3. **At-least-once + dedupe, not exactly-once.** Neither broker echoes a client key, so the engine
   owns `client_order_id ↔ broker_order_id` and dedupes before routing (D8/§A).
4. **Durable state before the venue.** Persist to `execution.*`, then route; reconcile drift against
   broker truth (§B).
5. **Capabilities declared, enforced up front.** Reject an unsupported `(broker, market, order_type,
   tif, position_effect)` with a typed error before any venue I/O (D7).

Full rationale: [umbrella ADR `feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md).

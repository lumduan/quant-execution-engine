# Architecture — Adapters & Capability Matrix

A **broker is an adapter, not a strategy change.** Every venue implements one `BrokerAdapter`
interface behind the unchanged `NormalizedOrder`; the router enforces each adapter's declared
capabilities up front (D7). This page is the abstraction and the matrix. For the lifecycle see
[`state-machine.md`](state-machine.md); for credentials see [`security-boundary.md`](security-boundary.md).

## The `BrokerAdapter` interface

Source: `src/quant_execution_engine/adapters/base.py`. Seven methods (frozen Phase 0 §D):

| Method | Returns | Purpose |
|--------|---------|---------|
| `place(order)` | `PlaceAck` | Submit a `NormalizedOrder`; ack carries `broker_order_id` + any immediate fills (or a reject) |
| `cancel(client_order_id)` | `CancelAck` | Cancel a resting order |
| `amend(client_order_id, new_price=, new_qty=)` | `AmendAck` | Amend price/qty; `AmendAck.semantics` is `"native"` or `"cancel_replace"` |
| `get_open_orders(account)` | `list[NormalizedOrder]` | Venue truth for reconciliation |
| `get_positions(account)` | `list[Position]` | Position snapshot |
| `get_account(account)` | `AccountInfo` | Account snapshot |
| `capabilities()` | `tuple[CapabilitySet, ...]` | The declared support cells |

Each real adapter additionally runs a session **heartbeat** + **circuit breaker** and a
**reconciliation loop** (not part of the frozen seven — an adapter-runtime concern). The breaker
trips on consecutive heartbeat failures → `broker_circuit_open` (503) + a mass-cancel of that
broker's open orders.

## Capability matrix — Liberator vs Streaming Pro vs Sim

> **Updated 2026-07-18 — broker-023 / `settrade_v2` (Settrade Open API) removed.** The real brokers
> are now Liberator + Streaming Pro (the self-built retail bridge); the Settrade Open-API column is
> gone. Terminology: "Streaming Pro"/`streaming_pro` = KEPT bridge; "Settrade"/`settrade_v2` =
> REMOVED Open API.

The router rejects an unsupported `(broker, market, order_type, tif, position_effect)` with a typed
`capability_unsupported` (422) **before any venue I/O**. `GET /capabilities` returns the narrow
per-`(broker, market)` `CapabilitySet` cells (order types, TIFs, position effects, amend semantics,
`adapter_installed`); the broader matrix below (auth, cancel, query, stream, idempotency) is the
canonical cell-level reference (mirrors `.claude/knowledge/capability-matrix.md`).

| Capability | Liberator (SET / TFEX) | Streaming Pro (SET + TFEX) | Sim |
|---|---|---|---|
| Auth | OTP/2FA + SMS refresh + Redis token; per-order PIN | bridge-owned login/OTP/session; engine holds only the bridge api-key — **no PIN** (the bridge stamps it) | none |
| Markets | SET + TFEX | SET (`fis`) + TFEX (`seosd`) via the retail bridge | any |
| `side` | SET Buy/Sell; TFEX Long/Short | SET Buy/Sell; TFEX Long/Short | both |
| `position_effect` | TFEX Open/Close; SET n/a | TFEX Open/Close; SET n/a | TFEX Open/Close |
| MARKET / LIMIT | ✅ | ✅ (only these live-verified) | ✅ |
| STOP / STOP_LIMIT | TFEX ✅; **SET ✗** | ✗ (conservative v1) | ✅ |
| ICEBERG | ✅ | ✗ (conservative v1) | ✅ |
| ATO / ATC | SET ✅; TFEX ✗ | ✗ (conservative v1) | ✅ |
| MTL | SET ✅; TFEX ✗ | ✗ (conservative v1) | ✅ |
| TIF | Day / GTC / IOC / FOK | Day only (conservative v1) | all |
| **Amend** | ✗ no route → **cancel+replace** (non-atomic, declared) | ✗ bridge `/order/change` 501 → **cancel+replace** (non-atomic) | native |
| Cancel | orderNo list (≤50) + PIN | bridge cancel (bridge-stamped PIN) | ✅ |
| Reconcile query | `GET /orders*` | bridge `fetch_venue_orders` | in-process |
| Order-update stream | indirect (reconciler-fed) | reconciler-fed | synthetic |
| Client idempotency key | ✗ | ✗ | n/a |

> Verified against `contracts/capabilities.py` (the installed `CapabilitySet` rows): SIM SET/TFEX =
> all 8 order types + all 4 TIFs (native amend); LIBERATOR SET = MARKET/LIMIT/ICEBERG/MTL/ATO/ATC
> (cancel_replace), LIBERATOR TFEX = MARKET/LIMIT/STOP/STOP_LIMIT/ICEBERG (cancel_replace);
> STREAMING_PRO SET + TFEX = MARKET/LIMIT × DAY (cancel_replace, conservative v1). `position_effects=()`
> for SET, `(OPEN, CLOSE)` for TFEX; all rows `adapter_installed=true`.

### The two structural consequences

1. **Engine-owned idempotency.** Neither real broker accepts a client idempotency key, so the engine
   persists the `client_order_id ↔ broker_order_id` mapping and dedupes before routing. "Exactly-once-ish"
   = dedupe + durable state + reconcile + safe re-submit (not true exactly-once).
2. **Amend is cancel+replace for every real broker.** `BrokerAdapter.amend()` is uniform;
   `LiberatorAdapter.amend` and `StreamingProAdapter.amend` both degrade to **cancel-then-replace**
   (declared non-atomic, returns a **new** `client_order_id`). **Native in-place amend was Settrade-only
   and left with broker-023** — among current brokers only the `sim` simulator declares native (atomic,
   same `client_order_id`). Callers read `GET /capabilities` to learn the semantics — they never assume
   them. See [`../api/orders-amend.md`](../api/orders-amend.md).

## SimAdapter

In-process, deterministic, zero broker I/O — the default-stage adapter and the test workhorse.

- **Deterministic fills** via the order's `metadata` control channel: `sim_reject` (a string) makes
  `place()` return a reject; `sim_fills` (a list of integer quantities) scripts partial fills in
  order; absent `sim_fills` ⇒ one full fill; an empty list ⇒ the order rests at `NEW` (cancellable).
  FOK requires an exact full fill; an IOC remainder is marked cancelled.
- **Live fill pricing chain (D21)**, all limit-bounded: **order-book best bid/offer → market-data
  engine last close → reference price** (the order's own `price`/`stop_price`/default). With no price
  source injected the sim is **bit-for-bit the Phase-2 deterministic sim**. Source:
  `adapters/sim.py`, `adapters/sim_pricing.py`.

## LiberatorAdapter

The **first real venue** (Phase 3). Composed **over** the bundled `liberator-trading-api` HTTP
service — it never re-implements it (D9).

- **Transport:** a redacting `httpx` client to `liberator-trading-api` (internal `:8200`); PIN and
  account never logged.
- **Amend:** declared `cancel_replace` (Liberator has no amend route). The router cancels the old
  order down the `PENDING_CANCEL` path and submits a fresh replacement (new `client_order_id`); no
  PTRM exemption on the replacement.
- 🔴 **The place-ack carries NO `orderNo`** — unconditionally, measured 2026-08-25 on both a
  terminal-on-arrival FOK and a DAY LIMIT that rested. The handle exists only in
  `GET orders/{account}`. This is the venue's normal behaviour, **not** a fault or a lost ack.
- **Handle recovery (TK-0423):** because of the above, `_place_and_settle` bursts against venue truth
  when the ack has no handle — 250 ms cadence (≈ the bridge read), 1500 ms budget anchored on the
  **submit** timestamp (the venue answers *before* our POST returns). It stops on handle-recovered,
  never on terminal — a resting order has none. The submit response reports
  `resolution: confirmed | pending | unknown`; see [`../api/orders-submit.md`](../api/orders-submit.md).
- **Reconcile loop v1:** §B lost-ack fuzzy match (±5 s), bounded resolution (~60 s). Shares its
  matcher and executor with the burst above — one implementation, two entry points, so they cannot
  drift.
- **Heartbeat + breaker:** ~30 s `GET order/health/*` liveness probe; consecutive failures trip
  `broker_circuit_open` + mass-cancel.
- **Deployment:** bundled via `docker-compose.liberator.yml` (internal-only, its own `liberator-redis`
  sidecar). See [`../operations/bring-up.md`](../operations/bring-up.md).

> **The `SettradeAdapter` (Settrade Open API, broker-023 / `settrade_v2`) was REMOVED on 2026-07-18.**
> It is not documented here as a live adapter. See
> [`../../.claude/knowledge/decision-log.md`](../../.claude/knowledge/decision-log.md) → the
> 2026-07-18 removal entry. The current real venues are LiberatorAdapter (above) and
> StreamingProAdapter (below).

## StreamingProAdapter

The **retail-bridge venue** (Phase 8) — SET + TFEX via the self-built `settrade-streaming-api`
bridge. **"Streaming Pro" is the self-built bridge, NOT the removed Settrade Open API.**

- **Transport:** plain `httpx.AsyncClient` composing the bundled `settrade-streaming-api` bridge
  (host `:8700`) — mirrors `LiberatorAdapter` (composed over an HTTP service, never re-implemented).
  The engine holds **only** the bridge's api-key + base URL; the **bridge** owns
  USERNAME/PASSWORD/PIN and stamps the PIN, so the adapter sends **no PIN**.
- **Amend:** declared **cancel_replace** (the bridge's native `/order/change` returns 501). The
  router cancels the old order down the `PENDING_CANCEL` path and submits a fresh replacement (new
  `client_order_id`); no PTRM exemption on the replacement.
- **Capability cells:** conservative — `(MARKET, LIMIT) × DAY` for SET + TFEX; cells expand as
  live-verified.
- **Heartbeat + breaker:** the bridge's `/session/status` liveness probe; consecutive failures trip
  `broker_circuit_open` + mass-cancel.
- **Reconcile loop v1:** mirrors Liberator (watermark fills; §B lost-ack/bounded resolution).
- **Deployment:** bundled via `docker-compose.streaming.yml` (the bridge + its `streaming-pro-redis`
  sidecar). See [`../operations/bring-up.md`](../operations/bring-up.md).

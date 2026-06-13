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

## Capability matrix — Liberator vs Settrade vs Sim

The router rejects an unsupported `(broker, market, order_type, tif, position_effect)` with a typed
`capability_unsupported` (422) **before any venue I/O**. `GET /capabilities` returns the narrow
per-`(broker, market)` `CapabilitySet` cells (order types, TIFs, position effects, amend semantics,
`adapter_installed`); the broader matrix below (auth, cancel, query, stream, idempotency) is the
canonical cell-level reference (mirrors `.claude/knowledge/capability-matrix.md`).

| Capability | Liberator (SET / TFEX) | Settrade (SET + TFEX) | Sim |
|---|---|---|---|
| Auth | OTP/2FA + SMS refresh + Redis token; per-order PIN | OAuth app creds → token (ECDSA P-256 login sig, single-flight refresh); per-order PIN. **May be split per market across two apps** (Phase 4.1) | none |
| Markets | SET + TFEX | SET (`/api/seos/v3`) + TFEX (`/api/seosd/v3`) | any |
| `side` | SET Buy/Sell; TFEX Long/Short | SET Buy/Sell; TFEX Long/Short | both |
| `position_effect` | TFEX Open/Close; SET n/a | TFEX Open/Close; SET n/a | TFEX Open/Close |
| MARKET / LIMIT | ✅ | ✅ (`MP-MKT` / `Limit`) | ✅ |
| STOP / STOP_LIMIT | TFEX ✅; **SET ✗** | TFEX ✅; **SET ✗** | ✅ |
| ICEBERG | ✅ | ✅ (SET `qtyOpen` / TFEX `icebergVol`) | ✅ |
| ATO / ATC | SET ✅; TFEX ✗ | SET ✅; TFEX `ATO` ✅, **`ATC` ✗** | ✅ |
| MTL | SET ✅; TFEX ✗ | SET + TFEX ✅ (`MP-MTL`) | ✅ |
| TIF | Day / GTC / IOC / FOK | Day / GTC / IOC / FOK (`Date`/GTD ✗) | all |
| **Amend** | ✗ no route → **cancel+replace** (non-atomic, declared) | ✅ **native** `PATCH .../change` (`PENDING_REPLACE → NEW`) | native |
| Cancel | orderNo list (≤50) + PIN | `PATCH .../cancel` + bulk `PATCH /cancel` + PIN | ✅ |
| Reconcile query | `GET /orders*` | `GET /orders` + `GET /trades` | in-process |
| Order-update stream | indirect (reconciler-fed) | reconciler-fed (native MQTT push deferred, §I/D23) | synthetic |
| Client idempotency key | ✗ | ✗ | n/a |

> Verified against `contracts/capabilities.py` (the installed `CapabilitySet` rows): SIM SET/TFEX =
> all 8 order types + all 4 TIFs (native amend); LIBERATOR SET = MARKET/LIMIT/ICEBERG/MTL/ATO/ATC
> (cancel_replace), LIBERATOR TFEX = MARKET/LIMIT/STOP/STOP_LIMIT/ICEBERG (cancel_replace); SETTRADE
> SET = MARKET/LIMIT/MTL/ATO/ATC/ICEBERG (native), SETTRADE TFEX =
> MARKET/LIMIT/MTL/ATO/STOP/STOP_LIMIT/ICEBERG (native). `position_effects=()` for SET, `(OPEN, CLOSE)`
> for TFEX; all rows `adapter_installed=true`.

### The two structural consequences

1. **Engine-owned idempotency.** Neither broker accepts a client idempotency key, so the engine
   persists the `client_order_id ↔ broker_order_id` mapping and dedupes before routing. "Exactly-once-ish"
   = dedupe + durable state + reconcile + safe re-submit (not true exactly-once).
2. **Asymmetric amend.** `BrokerAdapter.amend()` is uniform, but `SettradeAdapter.amend` is **native**
   (atomic, same `client_order_id`) while `LiberatorAdapter.amend` degrades to **cancel-then-replace**
   (declared non-atomic, returns a **new** `client_order_id`). Callers read `GET /capabilities` to learn
   the semantics — they never assume them. See [`../api/orders-amend.md`](../api/orders-amend.md).

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
- **Reconcile loop v1:** §B lost-ack fuzzy match (±5 s), bounded resolution (~60 s).
- **Heartbeat + breaker:** ~30 s `GET order/health/*` liveness probe; consecutive failures trip
  `broker_circuit_open` + mass-cancel.
- **Deployment:** bundled via `docker-compose.liberator.yml` (internal-only, its own `liberator-redis`
  sidecar). See [`../operations/bring-up.md`](../operations/bring-up.md).

## SettradeAdapter

The **second real venue** (Phase 4) — full **SET equity + TFEX derivatives** — proving the
abstraction.

- **Transport:** a **raw `httpx.AsyncClient` OAuth client**, deliberately **not** the sync
  `settrade-v2` SDK for order routing (E21). Why: the SDK is `requests`-based (sync — it blocks the
  event loop, violating async-first) and carries import-time side effects; the engine re-implements
  the auth recipe (ECDSA P-256 login signing, single-flight `ensure_token()` with proactive refresh,
  refresh-fail → fresh login, one reactive-401 retry).
- **Amend:** **native** over the frozen `PENDING_REPLACE → NEW` edge (one atomic `replace_order`); a
  venue amend-reject is a **non-terminal** restore + typed `AmendRejected` (409) — the order stays
  live.
- **Per-market broker apps (Phase 4.1):** one `SettradeClient` per market behind the unchanged
  adapter, so a broker that splits its books across two OAuth apps (InnovestX `023`: `ALGO_EQ` = SET,
  `ALGO` = TFEX) routes both legs concurrently. A partial per-market trio fails loud; one dead app
  trips the single breaker and mass-cancels both books.
- **Heartbeat:** the venue has no health endpoint, so the heartbeat is an OAuth **token-liveness**
  probe (`ensure_token()` per distinct client).
- **Reconcile loop v1:** mirrors Liberator + the `replace_resolve` action for stranded
  `PENDING_REPLACE`. No compose overlay (cloud API; creds ride `docker-compose.private.yml`).

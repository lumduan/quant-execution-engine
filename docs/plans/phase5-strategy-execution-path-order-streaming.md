# Phase 5: Strategy Execution Path + Order-Update Streaming + Dual-Provider Order Book

**Feature:** `feature-execution-engine` — Phase 5 (engine side)
**Branch:** `feature/phase5-strategy-execution-path-order-streaming`
**Created:** 2026-06-12
**Status:** Complete (engine side)
**Completed:** 2026-06-12
**Depends On:** Phase 4.1 (Complete, 2026-06-11)

---

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Streaming Event Schema](#streaming-event-schema)
6. [Order Book Contract](#order-book-contract)
7. [Implementation Steps](#implementation-steps)
8. [Existing Tests That Change](#existing-tests-that-change)
9. [Rollout Order](#rollout-order)
10. [File Changes](#file-changes)
11. [Success Criteria](#success-criteria)
12. [Open Questions / Deferred (§H–§K)](#open-questions--deferred-h-k)
13. [Completion Notes](#completion-notes)
14. [Prompt](#prompt)

---

## Overview

### Purpose

Phase 5 wires strategies as first-class callers of the execution engine and ships the
normalized **order-update stream out** (umbrella D12): fills, rejects, and every state
transition pushed to subscribed strategies over SSE, with no polling. It additionally ships a
**dual-provider order book service** (Settrade realtime + Liberator WebSocket) — a normalized,
read-only L2 cache that (a) gives the `SimAdapter` realistic best-bid/offer paper-fill prices,
(b) serves snapshot + SSE endpoints for spread/arbitrage strategies, and (c) seeds future
market-impact modelling.

Per operator decision (2026-06-12), this phase is **engine-side only**: the strategy-repo
execution flags (`CSM_EXECUTION_MODE`, `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE`) and the
end-to-end sim trade loop move to **Phase 5.1** in the strategy repos. A second operator
decision persists `strategy_id` durably (additive `quant-infra-db` migration) so stream
filtering survives engine restarts.

### Parent Plan Reference

- `docs/plans/ROADMAP.md` — Phase 5
- Umbrella: `plans/feature-execution-engine/ROADMAP.md`; ADR
  `.claude/knowledge/feature-execution-engine.md` (D1–D13, §A–§G)

### Key Deliverables

1. **`src/quant_execution_engine/events/`** — `OrderUpdateEvent` schema + in-process
   `EventHub` (ring buffer, bounded per-subscriber queues, monotonic seq)
2. **Publish hooks** in the five repository write functions (`insert_order`, `ack_order`,
   `replace_order`, `update_status`, `apply_fill`) — the proven choke points all 13 frozen
   edges funnel through
3. **`GET /orders/stream`** — SSE, filterable by `strategy_id` / `client_order_id`,
   `Last-Event-ID` reconnect, 15 s keep-alive
4. **Strategy identity** — `X-Strategy-Id` header stamped onto `execution.orders.strategy_id`
   (new nullable column, own `quant-infra-db` PR)
5. **`src/quant_execution_engine/order_book/`** — normalized `OrderBook`/`OrderBookLevel`
   models, asyncio-safe LRU cache service, Settrade + Liberator providers, failover router,
   lifespan runtime
6. **`GET /order-book/{symbol}`** (snapshot) + **`GET /order-book/{symbol}/stream`** (SSE)
7. **`SimAdapter`** fill prices from the order book cache with a documented fallback chain
8. **Gateway streaming proxy** (own `quant-api-gateway` PR) — non-buffering SSE pass-through
9. Docs: ROADMAP, CLAUDE.md, `.claude/knowledge/` (event schema, order book architecture,
   failover contract, decision log D14–D24), umbrella ADR addendum

---

## AI Prompt

This phase was generated from an operator prompt; the **full verbatim text** is preserved in
the [Prompt](#prompt) section at the end of this document (required by the prompt itself).
Two scope choices were confirmed interactively before planning: (1) persist `strategy_id`
via an additive infra-db migration; (2) engine-side only — strategy-repo flags become
Phase 5.1.

---

## Scope

### In Scope (Phase 5, this repo + two support PRs)

| Component | Description | Status |
|---|---|---|
| `events/models.py` | `OrderUpdateEvent` — strict subset of the frozen 9-state machine | Complete |
| `events/hub.py` | `EventHub`: non-blocking publish, ring buffer, per-subscriber bounded queues | Complete |
| Repository publish hooks | 5 write functions emit events post-success; order path never blocked | Complete |
| `strategy_id` stamping | `X-Strategy-Id` header → `execution.orders.strategy_id` (nullable) | Complete |
| `GET /orders/stream` | SSE; filters; `Last-Event-ID` replay; keep-alive comments | Complete |
| `order_book/models.py` | `OrderBookLevel` / `OrderBook` (frozen Pydantic, `Decimal`, UTC) | Complete |
| `order_book/service.py` | LRU cache (max 500 symbols, 5 s max-age), per-symbol streams | Complete |
| `order_book/providers/settrade.py` | `subscribe_bid_offer` → asyncio bridge; lifecycle mgmt | Complete |
| `order_book/providers/liberator.py` | ws-ticket + `websockets` Engine.IO client + reconnect | Complete |
| `order_book/router.py` | primary/secondary failover (N errors in window) + structured log | Complete |
| `order_book/runtime.py` | process singleton, lifespan start/stop, default **off** | Complete |
| `GET /order-book/{symbol}[/stream]` | snapshot (404 cold) + SSE | Complete |
| `SimAdapter` price source | book cache → market-data engine → limit/stop/default | Complete |
| `quant-infra-db` PR | `15_*.sql`: nullable `execution.orders.strategy_id` + partial index | PR #15 open |
| `quant-api-gateway` PR | streaming proxy (`client.stream()`), PATCH proxy verified/added | Pending (own PR) |

### Out of Scope (Phase 5)

- Strategy-repo execution flags + sim trade loop (`CSM_EXECUTION_MODE`,
  `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE`) — **Phase 5.1**, own repos/PRs
- Any change to `live`/`micro_live` gating, the kill-switch path, PTRM, or the frozen
  `NormalizedOrder` contract / 13-edge state machine / capability cells
- Per-strategy API keys / JWT (least-privilege auth) — §J
- Settrade native order-push as a *direct* transition source; `GET /trades` per-fill
  granularity — §I (this phase ships at most a reconcile-"kick")
- Multi-worker / cross-process fan-out (Redis pub/sub) — §H
- Durable order-book storage, market-impact modelling, depth > 10 — §K

---

## Design Decisions

Numbering continues the umbrella Decision Log (D1–D13, frozen in Phase 0).

### D14 — SSE, not WebSocket; hand-rolled, no new dependency

Order updates are strictly server→client; nothing flows upstream mid-stream. SSE over
`StreamingResponse(media_type="text/event-stream")` is stateless, upgrade-free, and
proxy-friendly (the gateway forwards a plain chunked HTTP response), and the browser/client
reconnect convention (`Last-Event-ID`) matches our ring-buffer replay. WebSocket would buy
bidirectionality we don't need at the cost of upgrade handling in the gateway proxy.
`sse-starlette` is declined: the keep-alive comment + disconnect handling is ~30 lines we can
type strictly ourselves, and one fewer dependency on the real-money path.

### D15 — In-process `EventHub` hooked inside the repository write functions

Exploration confirmed every one of the 13 frozen edges funnels through exactly four functions
in `db/repositories.py` (`ack_order`, `replace_order`, `update_status`, `apply_fill`), plus
`insert_order` for the `PENDING_NEW` birth — call sites in `core/router.py` (submit, fills,
IOC walk, cancel, native amend, **kill-switch mass-cancel**) and both reconcilers. Hooking
these five functions guarantees **no transition is missed**. Publish is post-success,
synchronous, and non-blocking (`put_nowait`; a full subscriber queue drops oldest and enqueues
a `gap` advisory): the stream can never block, slow, or fail the order path. Ring buffer
(default 1024) with a process-monotonic `seq` = SSE event id; reconnect replays from the
buffer, otherwise the client receives `resync_required` and re-reads `GET /orders/{cid}`.
**The stream is advisory; the durable store is truth** — the streaming analogue of §A's
at-least-once + reconcile doctrine.

### D16 — Strategy identity = `X-Strategy-Id` header, persisted; shared API key unchanged

Strategies identify per request with an `X-Strategy-Id` header on `POST /orders`; the engine
stamps it into the new nullable `execution.orders.strategy_id` column (own infra-db PR; the
frozen `NormalizedOrder` contract is untouched — identity is transport metadata, not order
data). Events echo `strategy_id`; `GET /orders/stream?strategy_id=` filters on it, seeding
historical cids from the store at subscribe time so reconciler-discovered events for
pre-restart orders still match. Auth remains the existing shared `X-API-Key`
(hmac-compared); per-strategy keys/JWT are deferred (§J) — the header is trusted the same way
the order payload is.

### D17 — The order book service is an in-engine, read-only, lossy-tolerant consumer cache

ADR §G ("streaming creep") pinned D1: market-data streams are *external, read-only
dependencies*. This service stays inside that carve-out: it **consumes** broker feeds
read-only, holds an **in-memory** cache only (no durable storage, no canonical ownership —
`quant-marketdata-engine` remains the OHLCV owner), and a dropped tick is a resubscribe, never
a loss. Its purpose is execution-plane: paper-fill realism now, PTRM price-band checks later.
The umbrella ADR gains an addendum note rather than an edge change.

### D18 — Settrade order-book source = `settrade-v2` SDK realtime connection, contained

The bid/offer feed rides the SDK's MQTT-backed `subscribe_bid_offer(symbol, on_message)`.
The **E21 ban on the SDK stands, unchanged, for the order-routing path** (sync `requests`
blocks the loop). The realtime connection is different: its network loop runs on the SDK's own
thread and delivers sync callbacks, which we bridge with
`loop.call_soon_threadsafe(queue.put_nowait, msg)` — the event loop is never blocked (tested).
Containment rules: the SDK is imported **lazily inside `providers/settrade.py` only** (its
import-time side effects — `~/settradesdkv2_config.txt`, NTP call, version-check HTTP — are
documented and only fire when the order book service is enabled with a Settrade provider);
nothing outside the provider module may import it.

### D19 — Liberator order-book source = ws-ticket + raw `websockets` Engine.IO client

Ticket from the bundled `liberator-trading-api` (`POST /api/v1/ws-ticket` → fully-qualified
`wss://…/socket.io/?EIO=4&transport=websocket&api-key=…`), connected with the **`websockets`**
library — **`curl_cffi` is forbidden** (hard constraint: it caused frequent disconnections in
the legacy implementation). We implement the minimal Engine.IO v4 / Socket.IO client the feed
needs: read the `0{…}` open packet, answer server ping `2` with pong `3`, connect the
namespaces (the broker requires the default set `MarketStatusV2/TFEXDashboardV2/MarketIndexV2/
StockV2/TickerV2` before `BidOfferV2` — legacy finding, kept), resolve symbol → `orderBookId`
via the upstream `GET /order-book/{symbol}`, then batch-join rooms
(`42/BidOfferV2,["join","[id1,id2]"]`). Reconnect: exponential backoff with jitter, fresh
ticket per attempt, re-join all rooms on resume. The legacy 100 ms buffer-polling loop is
replaced by a plain `async for` recv loop.

### D20 — Provider failover: N consecutive errors within a window → silent switch + structured log

`EXECUTION_ENGINE_ORDER_BOOK_PRIMARY_PROVIDER = settrade | liberator` picks the default
primary; an optional per-symbol override map (`ORDER_BOOK_SYMBOL_OVERRIDES`, JSON) pins
symbols to a provider. When the active provider records ≥ `FAILOVER_ERROR_THRESHOLD`
consecutive errors within `FAILOVER_WINDOW_SECONDS`, subscriptions move to the secondary and a
structured `order_book_provider_switch` log event fires (old, new, symbol count, error count).
Consumers never notice: the normalized `OrderBook` is identical from either source. Recovery
back to primary is manual/restart (v1 — flapping protection beats auto-failback).

### D21 — Sim fill-price chain: book cache → market-data engine → existing reference price

`SimAdapter` gains an optional injected price source. When warm, a BUY fills at **best ask**, a
SELL at **best bid** — bounded by the order's limit price where one exists (a crossing limit
never fills through its own limit). When the cache is cold, fall back to the last close from
`quant-marketdata-engine` (`EXECUTION_ENGINE_MARKET_DATA_BASE_URL`, optional, plain httpx GET);
when that is absent/unreachable, fall back to the existing `_reference_price()`
(limit/stop/configured default). Every fallback hop logs a structured event. **Price-only**:
the `sim_fills`/`sim_reject` fill-plan semantics are untouched, and with no source injected the
adapter remains the Phase-2 deterministic pure function — existing tests and the frozen
acceptance behavior are preserved.

### D22 — Order book models are frozen Pydantic, not dataclasses

The prompt sketches `@dataclass`; the repo's boundary rule (umbrella hard rule 3: Pydantic at
module/external I/O boundaries) wins — these models cross the API boundary serialized. Frozen
`BaseModel`, `Decimal` prices, `int` volumes, tz-aware UTC `received_at`,
`Decimal`-as-string on the wire (matches the order contract).

### D23 — Settrade order-push = reconcile "kick" (stretch)

The ROADMAP names Settrade's native order push as the Phase-5 stream feeder. Mapping raw venue
push payloads directly onto the frozen edges would bypass the reconciler's hardened edge
enforcement (E20/E24), so v1 treats a push message as a **trigger only**: it schedules an
immediate reconcile pass for that account/market, and the reconciler — still the sole writer —
persists whatever truth it finds, which the D15 hooks then stream. Latency drops from the 12 s
poll to push-speed with zero new write paths. Ships only if the SDK realtime order
subscription proves clean in this phase; otherwise deferred under §I (the stream is already
fed by the reconciler + sim + router paths either way).

### D24 — Order-book endpoints are public-mode-readable; the whole service defaults OFF

`GET /order-book/*` carries no order data, no credential, no raw broker payload → readable in
public mode like the other reads. `EXECUTION_ENGINE_ORDER_BOOK_ENABLED` defaults **false**:
the service, its providers, and its SDK import never activate unless an operator opts in, so
the engine's existing behavior (and the broker-free `docker compose up` default) is bit-for-bit
unchanged. **No change to `live`/`micro_live` gating** — the feeds are read-only market data.

---

## Streaming Event Schema

A **strict subset of the frozen 9-state machine**: the SSE `event:` field is exactly the
engine state reached; no new states, no new edges.

```text
SSE frame
  id:    <seq>                     # process-monotonic int (ring-buffer index)
  event: <engine_state reached>    # PENDING_NEW | NEW | PARTIALLY_FILLED | FILLED
                                   # | PENDING_CANCEL | PENDING_REPLACE | CANCELLED
                                   # | REJECTED | EXPIRED
                                   # advisory frames: gap | resync_required
  data:  OrderUpdateEvent JSON

OrderUpdateEvent
  seq              : int
  client_order_id  : str
  strategy_id      : str | None
  engine_state     : (9-state, as above)
  status           : NormalizedStatus     # frozen 6-value public mapping
  broker           : str
  broker_order_id  : str | None
  fill             : { broker_fill_id, price: Decimal-as-string, quantity: int,
                       exec_ts: ISO-UTC } | None   # present on fill events only
  ts               : ISO-UTC
```

Hook → event mapping: `insert_order` → `PENDING_NEW` (the "accepted" event); `ack_order` →
`NEW`; `apply_fill` → `PARTIALLY_FILLED`/`FILLED` (+ `fill` payload); `replace_order` → `NEW`
(amended values); `update_status` → the target state (covers cancel walk, expiry, rejects,
amend restore, **kill-switch mass-cancel sweep**). Reconnect: client sends `Last-Event-ID`; if
still inside the ring buffer the gap replays in order, else one `resync_required` advisory is
sent and the client re-reads `GET /orders/{client_order_id}`. Keep-alive: `: keep-alive`
comment every `STREAM_KEEPALIVE_SECONDS` (default 15).

---

## Order Book Contract

```text
OrderBookLevel            OrderBook
  price  : Decimal          symbol      : str          # canonical ticker ("AOT", "S50H25")
  volume : int              market      : "SET"|"TFEX"
                            bid_levels  : list[OrderBookLevel]   # [0] = best bid, depth ≤ 10
                            ask_levels  : list[OrderBookLevel]   # [0] = best ask, depth ≤ 10
                            bid_flag    : str           # "NORMAL" | "CEILING" | "FLOOR" | …
                            ask_flag    : str
                            sequence    : int           # source-monotonic (Settrade seq / Liberator vs)
                            source      : "settrade" | "liberator"
                            received_at : datetime UTC
```

Identical regardless of source. Settrade parser: flat `bid_price1..10 / bid_volume1..10 /
ask_price1..10 / ask_volume1..10` + `bid_flag`/`ask_flag` dict from `subscribe_bid_offer`.
Liberator parser: `42/BidOfferV2,["update",{room, vs, bp, bv, op, ov, bmv?, omv?}]` — string
prices to `Decimal`, zero-price levels dropped, partial depth (< 10) and absent `bmv`/`omv`
tolerated. Cache: in-memory LRU keyed `(symbol, market)`, `max_symbols` 500, `max_age` 5 s
(stale snapshots are not served to `SimAdapter`; the REST snapshot reports its age).

### New settings (`EXECUTION_ENGINE_` prefix; `.env.example` updated)

| Setting | Default | Effect |
|---|---|---|
| `ORDER_BOOK_ENABLED` | `false` | Master switch for the whole service (D24) |
| `ORDER_BOOK_PRIMARY_PROVIDER` | `settrade` | `settrade \| liberator` |
| `ORDER_BOOK_SYMBOL_OVERRIDES` | `{}` | JSON map symbol → provider |
| `ORDER_BOOK_FAILOVER_ERROR_THRESHOLD` | `3` | consecutive errors before switch |
| `ORDER_BOOK_FAILOVER_WINDOW_SECONDS` | `30` | error-counting window |
| `ORDER_BOOK_CACHE_MAX_AGE_SECONDS` | `5` | snapshot freshness bound |
| `ORDER_BOOK_CACHE_MAX_SYMBOLS` | `500` | LRU capacity |
| `MARKET_DATA_BASE_URL` | unset | sim fallback hop 2 (last close) |
| `STREAM_KEEPALIVE_SECONDS` | `15` | SSE comment interval |
| `STREAM_RING_BUFFER_SIZE` | `1024` | replay window |
| `STREAM_SUBSCRIBER_QUEUE_SIZE` | `256` | per-subscriber back-pressure bound |

---

## Implementation Steps

In dependency order (= commit train; conventional commits per step):

1. **Plan doc** (this file) — `docs(plans)` commit, pre-code (Phase-4 precedent).
2. **infra-db PR** (`quant-infra-db`, own repo): `13_execution_strategy_id.sql` — additive
   `ALTER TABLE execution.orders ADD COLUMN strategy_id text NULL` + index
   `(strategy_id, created_at)`; idempotent re-apply; triggers/edges untouched.
3. **3A** `order_book/{models,errors,service}.py` + unit tests (Decimal round-trips,
   LRU/max-age, asyncio-safe update, refcounts).
4. **3B** `order_book/providers/{base,settrade}.py`; `pyproject.toml` gains `websockets` +
   `settrade-v2`; tests mock the SDK callback (shape, precision, loop-never-blocked,
   subscribe/unsubscribe lifecycle).
5. **3C** `order_book/providers/liberator.py`; tests feed raw `42/BidOfferV2` frames through
   the parser and a mock WS server through the connector (reconnect fires on disconnect,
   re-join after resume).
6. **3D** `order_book/router.py` + `order_book/runtime.py` + lifespan wiring in
   `api/main.py`; failover integration test (primary raises ⇒ secondary within timeout +
   structured log).
7. **3E** snapshot + SSE order-book routes in `api/routes.py`/`schemas.py`; additive
   `/health` `order_book` block; route tests.
8. **3F** `SimAdapter` price source + `api/deps.py`/`core/router.py` wiring + settings;
   fallback-chain tests (warm cache / cold→market-data / cold→limit).
9. **3G** `events/{models,hub}.py`; hooks in the five repository functions;
   `strategy_id` (header dep, `insert_order` param, `OrderRow`); `GET /orders/stream`;
   D23 kick if clean. Tests: PENDING_NEW→NEW→FILLED ordering, partial-fill sequence,
   kill-switch sweep events on stream, `Last-Event-ID` replay, `gap` on overflow.
10. **Gateway PR** (`quant-api-gateway`, own repo): `client.stream()` +`StreamingResponse`
    proxy for `GET /orders/stream` + `GET /order-book/{symbol}[/stream]`; verify/add
    `PATCH /orders/{cid}` proxy; buffering note in the gateway docs.
11. **3H docs** + quality gate + engine PR (see [Rollout Order](#rollout-order)).

---

## Existing Tests That Change

| Test file | Why |
|---|---|
| `tests/conftest.py` | `_reset_singletons` learns the order-book runtime + event-hub globals; new fixtures (fake provider, hub drain) |
| `tests/_fakes.py` | `FakePool` already records calls — add a tiny fake hub + fake order-book source for router/sim tests |
| `tests/test_adapters_sim.py` | `SimAdapter` ctor gains an optional price-source param (default `None` keeps every existing assertion green) |
| `tests/test_api_routes.py` | additive `/health` `order_book` block; new routes are new tests, existing assertions unchanged |
| `tests/test_db_repositories.py` | the five write functions now publish — assertions that events fire post-success and never raise into the order path |
| `tests/test_core_router_submit.py` (+ amend/cancel siblings) | only if ctor wiring changes signatures; expected: additive optional params, no behavioral edits |

Everything else (state machine, contracts, adapters, reconcilers, stage matrix) is untouched
by design — any red test there means a design violation, not a test to update.

---

## Rollout Order

1. Plan doc commit (engine branch) → 2. infra-db migration PR (merge + live-apply) →
3. engine sub-steps 3A→3G as the commit train on the feature branch →
4. full quality gate (`ruff check` + `format --check` + `mypy src tests` + `pytest`,
   ≥90 % on `adapters/`, state machine, `order_book/`, `events/`) →
5. engine PR → review → merge →
6. gateway streaming-proxy PR (depends on merged engine surface for integration smoke) →
7. umbrella: CLAUDE.md + ADR addendum + pin bumps (engine, infra-db, gateway) + memory update.

Rollback story: the order book service is **additive and default-off**; the stream endpoint is
**opt-in** (nothing consumes it yet); the sim fallback chain terminates in the Phase-2
behavior, so an unavailable order book changes nothing; the infra-db column is nullable and
unread by older engine builds. No real-money surface moved: `live` stays gated exactly as in
Phases 3–4.1.

---

## File Changes

| File | Action | Description |
|---|---|---|
| `src/quant_execution_engine/events/{__init__,models,hub,errors}.py` | CREATE | event schema + EventHub |
| `src/quant_execution_engine/order_book/{__init__,models,service,errors,router,runtime}.py` | CREATE | normalized models, cache service, failover, singleton |
| `src/quant_execution_engine/order_book/providers/{__init__,base,settrade,liberator}.py` | CREATE | provider protocol + both feeds |
| `src/quant_execution_engine/db/repositories.py` | MODIFY | publish hooks (5 fns); `insert_order` gains `strategy_id` |
| `src/quant_execution_engine/db/models.py` | MODIFY | `OrderRow.strategy_id` |
| `src/quant_execution_engine/core/router.py` | MODIFY | thread `strategy_id` through submit; optional sim price source wiring |
| `src/quant_execution_engine/adapters/sim.py` | MODIFY | optional price source, D21 chain |
| `src/quant_execution_engine/api/routes.py` | MODIFY | `/orders/stream`, `/order-book/{symbol}[/stream]`, health block |
| `src/quant_execution_engine/api/schemas.py` | MODIFY | order-book + health schemas |
| `src/quant_execution_engine/api/deps.py` | MODIFY | `X-Strategy-Id` dep; hub/order-book injection |
| `src/quant_execution_engine/api/main.py` | MODIFY | lifespan: order-book runtime start/stop |
| `src/quant_execution_engine/config/settings.py` | MODIFY | new settings table above |
| `pyproject.toml` / `uv.lock` | MODIFY | `websockets`, `settrade-v2` |
| `.env.example` | MODIFY | operator template for the new settings |
| `tests/…` | CREATE/MODIFY | mirrors the source layout (see tables above) |
| `docs/plans/phase5-strategy-execution-path-order-streaming.md` | CREATE | this document |
| `docs/plans/ROADMAP.md`, `CLAUDE.md`, `.claude/knowledge/*` | MODIFY | Phase-5 status + new knowledge docs (step 3H) |
| *(quant-infra-db)* `13_execution_strategy_id.sql` | CREATE | own PR |
| *(quant-api-gateway)* `src/api/v2/engines/execution.py` | MODIFY | own PR — streaming proxy |

---

## Success Criteria

- [ ] Sim order placed → `GET /orders/stream` emits `PENDING_NEW → NEW → FILLED` in order
      (the prompt's "ACCEPTED, then FILLED"); partial fills stream one event per fill
- [ ] Kill-switch engage → mass-cancel sweep events appear on the stream
- [ ] Client reconnect with `Last-Event-ID` inside the ring buffer misses nothing; outside it
      receives `resync_required`
- [ ] `?strategy_id=` filter delivers only that strategy's events, including
      reconciler-driven events for orders submitted before a restart (DB-seeded)
- [ ] Both parsers produce identical normalized `OrderBook` shapes; non-trivial `Decimal`
      precision round-trips byte-exact; partial depth + absent `bmv`/`omv` + string prices
      handled
- [ ] Failover: primary provider erroring ≥ N times in the window switches to secondary
      within the test timeout and logs `order_book_provider_switch`
- [ ] `GET /order-book/{symbol}` → 404 cold, snapshot warm; `/stream` emits on cache update,
      keep-alive comment ≥ every 15 s, clean client-disconnect teardown
- [ ] Sim fill price == best bid/offer when warm; market-data fallback then limit-price
      fallback fire (and log) when cold; with no source injected, Phase-2 behavior bit-exact
- [ ] Settrade SDK callback never blocks the event loop (tested); SDK imported only inside
      the provider module; **no `curl_cffi` anywhere**
- [ ] Existing 713-test suite still green; gate: `ruff check` + `ruff format --check` +
      `mypy src tests` (strict) + `pytest` ≥ 90 % on `adapters/`, state machine,
      `order_book/`, `events/`
- [ ] `live` gating, kill-switch ordering, PTRM, frozen contracts: zero diffs
- [ ] Gateway proxies the SSE endpoints without buffering (verified with a curl -N smoke)

---

## Open Questions / Deferred (§H–§K)

Continuing the ADR's pinned §A–§G:

- **§H — Single-process fan-out.** The `EventHub` is in-process; the engine runs one uvicorn
  worker (compose). Multi-worker / multi-instance fan-out (Redis pub/sub mirroring the
  kill-switch pattern) is deferred until a second worker exists. Pinned assumption, revisit in
  Phase 6.
- **§I — Settrade push as a transition source + `GET /trades`.** This phase ships at most the
  D23 reconcile-kick. Driving frozen edges directly from venue push payloads, and per-fill
  granularity from `GET /trades` (replacing E18/E24 watermark deltas), stay deferred until the
  push protocol is observed at micro_live.
- **§J — Least-privilege strategy auth.** Per-strategy API keys (or JWT claims) binding a key
  to a `strategy_id` — submit/read/stream scoped to own orders. Deferred; the shared-key +
  header model ships now, and the column makes the upgrade additive.
- **§K — Order-book extensions.** Durable book persistence, market-impact modelling, depth
  > 10, auto-failback to primary, a 4h/derived-feed story — all out of scope for the
  execution plane until a concrete consumer exists (D1/D17 discipline).

---

## Completion Notes

### Summary

**Engine side complete (2026-06-12).** Phase 5 shipped the normalized **order-update stream
out** (umbrella D12 realised) plus the **dual-provider order book service** and the
`SimAdapter` live fill-price chain — all additive and default-off, with `live`/`micro_live`
gating, the kill-switch path, PTRM, and the frozen `NormalizedOrder` / 13-edge state machine /
capability cells **unchanged**. Concretely:

- **`events/`** (D14/D15/D22) — frozen `OrderUpdateEvent` + `FillEvent` + `GapMarker`
  (`Decimal`-as-string wire; `status` derived via the one existing `to_public_status` mapping,
  E8); an in-process `EventHub` with a monotonic `seq`, ring-buffer `Last-Event-ID` replay,
  bounded per-subscriber queues (drop-oldest + `gap` marker), a cid→strategy LRU, and an
  exception-proof `publish` (the order path can never fail on stream plumbing).
- **Repository publish hooks** in the five write functions `insert_order` (the `PENDING_NEW`
  birth) / `ack_order` / `replace_order` / `update_status` / `apply_fill` — the proven choke
  points every one of the 13 frozen edges funnels through, including the kill-switch
  mass-cancel sweep; publish is post-success, non-blocking, and `apply_fill` publishes only for
  newly-inserted fills (redeliveries emit nothing).
- **`GET /orders/stream`** (`api/streams.py`) — SSE, `id:`=seq / `event:`=engine-state frames
  (strict 9-state subset + `gap`/`resync_required` advisories), conjunctive
  `strategy_id`/`client_order_id` filters, `Last-Event-ID` replay from the ring, keep-alive
  comments, no-buffering headers.
- **D16 strategy identity** — `X-Strategy-Id` header (slug-validated, 422 on violation) →
  the new nullable `execution.orders.strategy_id` column (infra-db PR #15); events echo it;
  the stream seeds a strategy's historical cids from the store at subscribe time so
  reconciler-discovered events for pre-restart orders still match (DB-seeded, restart-safe).
- **`order_book/`** (D17–D20, D22, D24) — normalized frozen `OrderBook`/`OrderBookLevel`,
  an in-memory LRU cache (max-symbols / max-age) with refcounted SSE fan-out, a **Settrade**
  provider (SDK realtime behind a lazy `_import_sdk` seam, all blocking SDK work on
  `asyncio.to_thread`, `call_soon_threadsafe` bridge — E21 order-routing SDK ban unchanged), a
  **Liberator** provider (ws-ticket + raw `websockets` Engine.IO v4 client, **no `curl_cffi`**,
  default-namespaces-then-`BidOfferV2` join order, mid-session live join/leave, jittered
  reconnect with a fresh ticket + re-join), a failover **router** (N consecutive errors in a
  window → secondary + structured `order_book.provider_switch`; per-symbol overrides; no
  auto-failback), and a lifespan-wired **runtime** (default off — bit-for-bit unchanged engine).
- **`GET /order-book/{symbol}`** (404 cold; market omitted probes SET→TFEX) + **`/stream`**
  (snapshot-then-updates SSE); additive `/health` `order_book` block.
- **`SimAdapter` live pricing** (D21) — `SimFillPricer` resolves the fill price book best
  ask/bid (limit-bounded) → market-data engine last 1d close (limit-bounded) → `None` (the
  adapter's own `_reference_price`); price-only, and with **no source injected the adapter is
  bit-for-bit Phase-2** (fill plans, FOK/IOC semantics untouched).
- **New deps:** `websockets`, `settrade-v2` (lazy, market-data-only). **New env knobs**
  (`EXECUTION_ENGINE_` prefix): `ORDER_BOOK_ENABLED` (default false), `ORDER_BOOK_PRIMARY_PROVIDER`,
  `ORDER_BOOK_SYMBOL_OVERRIDES`, `ORDER_BOOK_FAILOVER_{ERROR_THRESHOLD,WINDOW_SECONDS}`,
  `ORDER_BOOK_CACHE_{MAX_AGE_SECONDS,MAX_SYMBOLS}`, `MARKET_DATA_BASE_URL`, `MARKET_DATA_API_KEY`,
  `STREAM_{KEEPALIVE_SECONDS,RING_BUFFER_SIZE,SUBSCRIBER_QUEUE_SIZE}`.

**Gate:** 853 tests passed, 95.72% total coverage, `mypy --strict`, `ruff` clean. Three engine
commits — `3530fc4` (order book), `f1e8991` (endpoints + sim pricing), `92c30b5`
(events/stream) — atop the pre-code plan-doc commit `dcc84ee`. The infra-db `strategy_id`
migration is **PR #15 (open)** in `lumduan/quant-infra-db`; the gateway streaming-proxy lands
in its own PR after the engine PR merges.

**D23 deferred:** the Settrade native order-push reconcile-kick is **not** shipped — wiring the
SDK realtime *order* subscription as a transition source would breach the D18 SDK containment
(the SDK may not leak into the `adapters/` layer), so it moves to **§I**. The stream is already
fed by the reconciler + sim + router write paths regardless.

**Scope split (operator decision, 2026-06-12):** this phase is **engine-side only**. The
strategy-side scope — `CSM_EXECUTION_MODE` / `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE` flags and
the end-to-end strategy sim trade loop — moves to **Phase 5.1** in the strategy repos; the
original Phase-5 acceptance ("a strategy runs signal → `NormalizedOrder` → fill stream →
position entirely in sim") completes there.

### Issues Encountered

Five findings surfaced and were fixed during review:

1. **Liberator mid-session subscribe never joined its room.** A new symbol subscribed while
   the socket was already up was tracked but its `BidOfferV2` room was only joined at the next
   reconnect — silently starving the subscriber. Fixed by resolving + joining (and, on
   unsubscribe, leaving) the room **live on the open socket**.
2. **Default-namespace chatter spammed the parse-skip WARNING.** The joined default namespaces
   (`StockV2`/`TickerV2`/…) stream their own event frames continuously, which the generic
   parse-skip path logged as malformed. Fixed by **namespace-scoping** the warning — only a
   malformed frame *inside* `BidOfferV2` warns; everything else is silently ignored.
3. **Settrade SDK login / MQTT-connect / subscribe ran blocking on the event loop.** The SDK's
   `Investor` login, `RealtimeDataConnection().start()`, and even `import settrade_v2` itself
   (NTP + version-check HTTP) all do blocking network I/O. Fixed by moving every blocking SDK
   call onto **`asyncio.to_thread`**, with the sync callback bridged back via
   `loop.call_soon_threadsafe` (the loop is never blocked — asserted off-loop in tests).
4. **Route registration order captured `stream`.** Splitting `api/streams.py` out of
   `routes.py` initially registered the streams router *second*, so `/orders/{client_order_id}`
   matched `stream` as a path param and shadowed `/orders/stream`. Fixed by registering the
   **streams router first** so `/orders/stream` out-ranks `/orders/{cid}`.
5. **SSE consumption tests must drive the generators directly.** `TestClient` runs the route on
   a separate `anyio` portal loop, so a cross-loop `queue.put` from the test never wakes the
   route's consumer. The SSE tests therefore drive the async generators directly rather than
   through `TestClient` streaming.

Three more surfaced in the **live real-venue validation (2026-06-12, read-only; AOT + S50M26)**
and were fixed the same day (details in
[`order-book-service.md`](../../.claude/knowledge/order-book-service.md#real-venue-validation-2026-06-12-read-only-aot--set-s50m26--tfex)):

6. **The Liberator WS host serves an incomplete TLS chain** (leaf only) — every verifying
   client fails `CERTIFICATE_VERIFY_FAILED`. Fixed by bundling the public GlobalSign
   intermediate (`providers/liberator_ca_chain.pem`) into a certifi-based `wss://` context
   (+ the `ORDER_BOOK_LIBERATOR_EXTRA_CA_PEM` operator override). Verification stays ON.
   **Result: both symbols verified streaming live** (S50M26: 93 normalized updates in 25 s).
7. **The Settrade SDK realtime API differs from the sketch**: `RealtimeDataConnection` has no
   `start()` (the CallBacker starts on `Subscriber.start()`), and the callback payload is an
   `{"is_success", "data"}` envelope around a `BidOfferV3` protobuf dict whose prices are
   `google.type.Money` `{units, nanos}` objects. Fixed: envelope unwrap (+ rejection →
   `on_error`), exact-Decimal Money parsing.
8. **Settrade realtime is venue-gated**: with both per-market logins succeeding, the
   dispatcher rejects `subscribe_bid_offer` with `DISPATCH-UM-04` "User is inactive" —
   realtime streaming is not enabled for the InnovestX apps/user. **Operator prerequisite**
   (portal-side), analogous to the missing trading PIN for `micro_live`; the provider
   surfaces it as a typed failover signal, so the router fails over to Liberator as designed.

---

## Prompt

The full operator prompt for this phase, verbatim:

````text
You are implementing **Phase 5** of the `quant-execution-engine` service — a production FastAPI/Python 3.11 service (host `:8400`, sole broker
order-routing-credential owner) that is one submodule of a larger quant trading platform umbrella repo.

---

## Step 0 — Orient yourself before writing a single line of code

Read **all** of the following in order. Use Fable model at xHigh effort for every thinking, planning, and review step. Do not skip any file.

1. `CLAUDE.md` — umbrella system map, network contract, API versioning, cross-cutting rules
2. `quant-execution-engine/CLAUDE.md` — per-service agent context, constraints, phase history, memory
3. `quant-execution-engine/docs/plans/ROADMAP.md` — canonical roadmap; Phase 5 scope is defined here
4. `quant-execution-engine/.claude/knowledge/` — all files (state machine, contracts, order-routing-safety, etc.)
5. `strategies/csm-set/docs/plans/examples/phase1-sample.md` — mandatory format reference for the plan doc you will produce
6. `quant-execution-engine/third_party/liberator-trading-api/docs/ws_ticket_service.md` — WS ticket service for Liberator real-time streams (order book
feed uses this)
7. `~/apps/trading/trading-strategies-api/app/internal/order_book_service.py` — legacy reference implementation; study the architecture, **do not copy**
wholesale — in particular, do not replicate any `curl_cffi` WebSocket usage (it causes frequent disconnections); identify which parts are still
conceptually valid and which must be redesigned

Also run:
```bash
git -C quant-execution-engine log --oneline -20
grep -r "OrderBook\|order_book\|bid_offer\|BidOffer\|subscribe_bid" quant-execution-engine/src --include="*.py" -l
grep -r "SSEResponse\|EventSourceResponse\|websocket\|StreamingResponse" quant-execution-engine/src --include="*.py" -l
grep -r "strategy.*order\|order.*strategy\|execution_path" quant-execution-engine/src --include="*.py" -l

---
Step 1 — Create the feature branch

git -C quant-execution-engine checkout main && git -C quant-execution-engine pull
git -C quant-execution-engine checkout -b feature/phase5-strategy-execution-path-order-streaming

---
Step 2 — Write the implementation plan (Fable model, xHigh effort — NO CODE YET)

Produce a detailed plan as quant-execution-engine/docs/plans/phase5-strategy-execution-path-order-streaming.md. It must follow exactly the format in
strategies/csm-set/docs/plans/examples/phase1-sample.md.

The plan must address every item below. Think through architecture trade-offs before writing anything down.

2A — Phase 5 core: Strategy execution path + order-update streaming

The execution engine currently accepts orders from any HTTP client. Phase 5 wires strategies as first-class callers:

- Strategy registration — how strategies identify themselves to the engine (API key scope, strategy_id header, or JWT claim); least-privilege model
- Order-update streaming — strategies need real-time fills, rejects, and state transitions pushed to them without polling. Design the streaming transport:
Server-Sent Events (text/event-stream) is the default preference (stateless, proxy-friendly, no upgrade); WebSocket is acceptable if SSE cannot satisfy a
concrete requirement — justify the choice. The event schema must be a strict subset of the frozen 9-state order state machine already implemented.
- Streaming endpoint contract — GET /api/v2/engines/execution/orders/stream?strategy_id=<id> or a per-order variant; define auth, reconnect semantics,
keep-alive interval, and back-pressure handling
- Gateway proxy update — the gateway must proxy the streaming endpoint; note any chunked-transfer or buffering pitfalls for the API gateway (FastAPI/httpx
proxy must not buffer the entire response)
- SimAdapter integration — the sim adapter must emit order-update events into the streaming layer on every state transition, including partial fills
- LiberatorAdapter / SettradeAdapter — reconcile-loop events and real fill callbacks must fan out to any subscribed strategy streams for matching
client_order_ids

2B — Addition requirement: Dual-provider order book service

Order book data is needed for:
1. Accurate paper-trading simulation (best bid/offer for fill price estimation)
2. Arbitrage and spread strategies that require live L2 data
3. Future market-impact modelling

Design a normalized, dual-provider OrderBookService with these properties:

Normalized contract (internal model — must be identical regardless of source):
@dataclass
class OrderBookLevel:
    price: Decimal
    volume: int

@dataclass
class OrderBook:
    symbol: str          # canonical ticker (e.g. "AOT", "S50H25")
    market: str          # "SET" | "TFEX"
    bid_levels: list[OrderBookLevel]   # index 0 = best bid, depth ≤ 10
    ask_levels: list[OrderBookLevel]   # index 0 = best ask, depth ≤ 10
    bid_flag: str        # "NORMAL" | "CEILING" | "FLOOR" | ...
    ask_flag: str
    sequence: int        # monotonic sequence number from source
    source: str          # "settrade" | "liberator"
    received_at: datetime  # UTC

Settrade provider
- Source: subscribe_bid_offer(symbol, on_message) callback on the Settrade SDK
- The raw message shape is the flat bid_price1..10 / ask_price1..10 / bid_volume1..10 / ask_volume1..10 dict shown in the requirements
- Parser must extract flag fields (bid_flag, ask_flag) and build the normalized OrderBook
- The SDK callback is synchronous; bridge to asyncio via loop.call_soon_threadsafe or asyncio.run_coroutine_threadsafe — do not block the event loop
- Manage subscription lifecycle: subscribe on first consumer, unsubscribe when last consumer drops

Liberator provider
- Source: WebSocket connection via the Ticket Service (liberator-trading-api/docs/ws_ticket_service.md) — generate a ticket URL, connect with websockets
(stdlib-compatible, no curl_cffi), authenticate, then subscribe to the BidOfferV2 room for each symbol
- Raw message format: 42/BidOfferV2,["update",{"room":…,"vs":…,"bp":[…],"bv":[…],"op":[…],"ov":[…],"bmv":[…],"omv":[…]}]
  - bp/bv = bid prices/volumes (index 0 = best bid)
  - op/ov = offer (ask) prices/volumes
  - bmv/omv = market volumes (may be absent)
  - vs = sequence number
- Use the websockets library (already available or add to pyproject.toml); implement exponential-backoff reconnect with jitter; do NOT use curl_cffi
websockets
- Parser must handle partial arrays (depth < 10), absent bmv/omv, and string prices

Fallback / provider selection
- Configurable per-symbol or per-market: ORDER_BOOK_PRIMARY_PROVIDER=settrade|liberator env var, per-symbol override map optional
- If the primary provider's WebSocket disconnects or throws ≥ N consecutive errors within a configurable window, silently switch to the secondary provider
and emit a structured log event
- Expose GET /order-book/{symbol} (cached snapshot, no streaming) and GET /order-book/{symbol}/stream (SSE of updates) under /api/v2/engines/execution/
- In-memory LRU cache keyed by (symbol, market) — configurable max-age (default 5 s) and max-symbols (default 500)

Integration with SimAdapter
- When filling a paper order, the SimAdapter must consult the OrderBookService cache for best bid/offer rather than using a fixed price
- If no live order book is cached for the symbol, fall back to last-trade price from the market-data engine (HTTP call) or the order's limit price as a
last resort; log the fallback clearly

Plan doc must explicitly state:
- Decisions made (numbered D1, D2, … continuing from the last decision in the existing ROADMAP)
- Open questions / deferred items tagged §H, §I, … (continuing from existing tags)
- Which existing tests change and why
- Rollout order (which sub-component lands first, second, …)
- The full text of this prompt, verbatim, in a ## Prompt section at the end

---
Step 3 — Implement (Opus subagent for coding and tests)

After the plan doc is committed, implement in this order:

3A — Normalized order book models and service skeleton

- quant-execution-engine/src/order_book/models.py — OrderBookLevel, OrderBook (use Decimal for prices, datetime UTC for timestamps, strict mypy)
- quant-execution-engine/src/order_book/service.py — OrderBookService abstract base + in-memory cache; thread-safe (asyncio-safe) cache update path
- quant-execution-engine/src/order_book/exceptions.py — typed exceptions

3B — Settrade order book provider

- quant-execution-engine/src/order_book/providers/settrade.py
- Wraps subscribe_bid_offer; bridges sync callback → asyncio queue
- Subscription lifecycle: subscribe(symbol) / unsubscribe(symbol) coroutines
- Unit tests: mock the Settrade SDK callback; assert normalized OrderBook shape and Decimal precision; assert event-loop is never blocked

3C — Liberator order book provider

- quant-execution-engine/src/order_book/providers/liberator.py
- Ticket acquisition via the WS ticket service (see liberator-trading-api/docs/ws_ticket_service.md); connection via websockets; BidOfferV2 message
parser; reconnect loop
- Unit tests: mock the WS server; feed raw 42/BidOfferV2,… frames; assert correct parsed OrderBook; assert reconnect fires on disconnect

3D — Provider router + fallback logic

- quant-execution-engine/src/order_book/router.py — reads env config, routes subscriptions, handles failover; emits structured log on provider switch
- Integration test: primary raises, assert secondary takes over within timeout

3E — REST + SSE endpoints

- GET /order-book/{symbol} — snapshot from cache; 404 if no data yet
- GET /order-book/{symbol}/stream — SSE; each event is a JSON-serialized OrderBook; include keep-alive comments every 15 s; handle client disconnect
cleanly
- Register under the existing router prefix /api/v2/engines/execution/
- Tests: hit the snapshot endpoint; assert SSE stream emits events when cache updates

3F — SimAdapter fill-price integration

- Modify quant-execution-engine/src/adapters/sim.py (or wherever SimAdapter lives) to inject OrderBookService and query best bid/offer on each fill
- Fallback chain: order book cache → market-data engine HTTP → order limit price
- Tests: assert fill price equals best bid/offer when cache is warm; assert fallback fires when cache is cold

3G — Order-update streaming (Phase 5 core)

- Design and implement the streaming infrastructure for order state transitions
- GET /orders/stream endpoint — SSE or WebSocket (justify choice in plan); auth via existing API key; filterable by strategy_id and/or client_order_id
- Internal fan-out mechanism: wherever the order state machine transitions a state (sim fills, Liberator/Settrade reconcile callbacks, kill-switch
mass-cancel), publish an event to in-process subscribers
- Gateway proxy: verify the gateway's proxy of /api/v2/engines/execution/orders/stream does not buffer; add integration note
- Tests: place a sim order → assert streaming endpoint emits ACCEPTED, then FILLED events in order; test reconnect (client drops and reconnects, assert no
missed events within the reconnect window if a ring-buffer is used)

3H — Documentation + knowledge updates

Use Opus model for all doc/commit work.

Update or create:
- quant-execution-engine/docs/plans/ROADMAP.md — mark Phase 5 complete, update Phase 6 stub
- quant-execution-engine/CLAUDE.md — add Phase 5 to the phase history section; add order book service summary; add streaming endpoint to the API surface
table
- quant-execution-engine/.claude/knowledge/ — add or update files covering: order book service architecture, provider failover contract, streaming event
schema
- CLAUDE.md (umbrella) — update the Execution engine entry in the engine catalog and the optional features table to reflect Phase 5 complete + order book
service
- .claude/knowledge/feature-execution-engine.md — update cross-cutting ADR with Phase 5 decisions
- Project memory at /home/batt/.claude/projects/-home-batt-docker-quant-trading-system/memory/ — update project-execution-engine-bootstrap.md to reflect
Phase 5 complete; add any new feedback-*.md entries if architectural surprises arose

---
Step 4 — Quality gate (Fable model for review and error-fixing)

Run the full quality gate before committing:

cd quant-execution-engine
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src --strict
uv run pytest tests -x --cov=src --cov-report=term-missing

Coverage must remain ≥ 90% on adapters/, the order state machine, and the new order_book/ module. Fix all ruff and mypy errors — do not suppress with #
type: ignore without a documented reason. If a test fails, use Fable model to diagnose the root cause; do not patch over it.

---
Step 5 — Commit and PR (Opus model)

Stage only source, tests, docs, and updated CLAUDE.md files. Never stage .env, secrets, or auto-generated coverage artifacts.

git -C quant-execution-engine add src tests docs quant-execution-engine/CLAUDE.md
# also stage umbrella changes:
git add CLAUDE.md .claude/knowledge/feature-execution-engine.md

Commit message format (conventional commits):
feat(phase5): strategy execution path, order-update streaming, dual-provider order book

- POST /orders fan-out: fills/rejects/transitions stream to subscribed strategies via SSE
- GET /orders/stream: per-strategy SSE endpoint with reconnect semantics
- OrderBookService: normalized Decimal L2 cache (depth 10), Settrade + Liberator providers
- SettradeProvider: subscribe_bid_offer → asyncio bridge, subscription lifecycle management
- LiberatorProvider: WS ticket service + BidOfferV2 parser, websockets reconnect loop
- Provider failover: primary→secondary on consecutive errors, structured log on switch
- SimAdapter: fills now use live best bid/offer; fallback chain documented
- 7xx tests, NN% coverage

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Open a PR against main of quant-execution-engine. PR body must include:
- Summary (3–5 bullets)
- Architecture decisions made (the D-numbered decisions from the plan doc)
- Test plan checklist (golden path + edge cases: empty cache, provider failover, kill-switch sweep, reconnect, partial fill streaming)
- Rollback / gating notes (order book service is additive; streaming endpoint is opt-in; SimAdapter fallback chain means no regressions if order book is
unavailable)
- Link to quant-execution-engine/docs/plans/phase5-strategy-execution-path-order-streaming.md

After the PR is open, report the result as the ASCII box-drawing table format required by the global CLAUDE.md git reporting convention (columns: Repo |
Branch | Commit | GitHub).

---
Constraints and non-negotiables

- Python 3.11, uv run for all commands — never bare python/pip
- Strict mypy throughout — --strict flag, zero ignored errors without documented reason
- Decimal for all prices at system boundaries — never float
- UTC for all stored timestamps; Asia/Bangkok only for display/logging
- No curl_cffi WebSockets — use the websockets library for Liberator WS; this is a hard constraint (curl_cffi WS causes frequent disconnections in this
environment)
- live stage stays gated — order book + streaming features must not change the live/micro_live real-money gating logic introduced in Phases 3–4.1
- Additive API surface only — no breaking changes to existing /orders endpoints
- Submodule rule — if third_party/liberator-trading-api needs changes, commit there first (on main), push, then pin in the parent; never commit the parent
against an unpushed submodule SHA
- No real-money orders during development — the order book service connects to real feeds (read-only); the execution path remains gated as before
- Structured logging — all provider switches, reconnect attempts, cache misses, and streaming subscriber counts must emit structured log events (not bare
print)
- Tests must cover: normalized model round-trips with non-trivial Decimal precision, provider failover trigger, SSE stream event ordering, SimAdapter
fill-price fallback chain, and kill-switch mass-cancel events appearing in the order stream
````

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Fable 5)
**Status:** Complete (engine side)
**Completed:** 2026-06-12

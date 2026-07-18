# Order book service — architecture

> **SHIPPED in Phase 5 (engine side, 2026-06-12)** — an **in-engine, read-only, lossy-tolerant**
> L2 cache (decisions **D17–D22, D24** —
> [`decision-log.md`](decision-log.md#phase-5-realisation-decisions-d14d24-2026-06-12)). It
> **consumes** the Liberator WebSocket bid/offer feed, normalizes it to one shape, caches the
> best-of-book in memory, and serves (a) `SimAdapter` realistic paper-fill prices, (b) snapshot +
> SSE endpoints, and (c) a seed for future market-impact / PTRM price-band work. **Additive and
> default-off.** Source: `src/quant_execution_engine/order_book/`.
>
> **Updated 2026-07-18 — the Settrade order-book provider was removed with broker-023 /
> `settrade_v2`.** The service is now **single-provider (Liberator only)**; the `ProviderRouter`
> stays provider-generic (failover is dormant until a second feed exists). The `settrade-v2` SDK
> dependency is gone. See [`decision-log.md`](decision-log.md) → the 2026-07-18 removal entry.

## Two planes (D17 / D1)

This stays inside the ADR §G "streaming creep" carve-out: market-data / order-book streams are
**external, read-only dependencies** of the execution plane. The service holds an **in-memory
cache only** — no durable storage, no canonical ownership (`quant-marketdata-engine` remains the
OHLCV owner). A dropped tick is a resubscribe, never a loss. Its purpose is execution-plane:
paper-fill realism now, price-band checks later. **It changes no `live`/`micro_live` gating** —
the feeds are read-only.

## Normalized contract (D22)

Frozen Pydantic (`order_book/models.py`) — not dataclasses (they cross the API boundary
serialized): `Decimal` prices, `int` volumes, tz-aware UTC `received_at`, `Decimal`-as-string on
the wire. **Identical regardless of source** — the `source` tag names the provider (`liberator`).

```text
OrderBookLevel                 OrderBook
  price  : Decimal (>0)          symbol      : str          # canonical ticker ("AOT", "S50H25")
  volume : int   (>=0)           market      : Market ("SET" | "TFEX")
                                 bid_levels  : list[OrderBookLevel]   # [0] = best bid, depth ≤ 10
                                 ask_levels  : list[OrderBookLevel]   # [0] = best ask, depth ≤ 10
                                 bid_flag    : str   (default "NORMAL")  # "CEILING"|"FLOOR"|…
                                 ask_flag    : str   (default "NORMAL")
                                 sequence    : int          # source-monotonic
                                 source      : "liberator"
                                 received_at : datetime UTC
  # convenience: .best_bid / .best_ask (top level or None); .wire_dump()
```

## Providers

A `OrderBookProvider` base (`providers/base.py`) defines `start/stop/subscribe/unsubscribe`
plus `on_book` / `on_error` callbacks; the router fans subscriptions to the active provider.
Liberator is the sole provider (the Settrade SDK-realtime provider, D18, was removed with
broker-023 / `settrade_v2` on 2026-07-18).

### Liberator — ws-ticket + raw `websockets` Engine.IO v4 (D19)

**Hard constraint: no `curl_cffi` anywhere** (frequent disconnects in the legacy
implementation). A minimal Engine.IO v4 / Socket.IO client (`providers/_engineio.py`, no
socket.io dependency):

1. **Fresh ws-ticket** per connection — `POST {liberator}/ws-ticket` (`api-key` header) → a
   fully-qualified `wss://…` `ws_url` that is a **credential** (only its host is logged).
2. Connect with the `websockets` asyncio client; read the `0{…}` Engine.IO open packet; answer
   server ping `2` with pong `3`.
3. **Connect the default namespaces BEFORE `BidOfferV2`** —
   `MarketStatusV2 / TFEXDashboardV2 / MarketIndexV2 / StockV2 / TickerV2` then `BidOfferV2` (a
   kept legacy finding — the broker requires the default set first).
4. Resolve each symbol → `orderBookId` (`GET {liberator}/order-book/{symbol}`, cached) and
   **batch-join rooms** (`42/BidOfferV2,["join","[id…]"]`).
5. `async for` recv loop emits normalized books; parses
   `42/BidOfferV2,["update",{room, vs, bp, bv, op, ov, bmv?, omv?}]` — string prices → `Decimal`,
   zero-price levels dropped, partial depth (< 10) and absent `bmv`/`omv` tolerated.
6. **Mid-session live join/leave** on the open socket — a symbol subscribed while the session is
   up is resolved + room-joined immediately (a session-start-only join silently starved
   mid-session subscribers until the next reconnect — a review finding); `leave` on unsubscribe.
7. **Reconnect:** exponential backoff (base 1 s, cap 60 s) **with jitter**, a fresh ticket per
   attempt, re-join all rooms on resume, backoff reset after a healthy period (~30 s).

The joined default namespaces stream their own event frames continuously — those are silently
ignored; only a malformed frame **inside** `BidOfferV2` earns a parse-skip WARNING
(namespace-scoping that warning was a review finding — default-namespace chatter had spammed it).

## Failover router (D20)

> **Dormant since 2026-07-18** — with Settrade removed the order book has a single provider
> (Liberator), so no secondary exists and failover cannot trigger. `ProviderRouter` is retained
> **generic** for a future second feed; the description below is its unchanged behaviour once a
> second provider is configured.

`order_book/router.py`. `ORDER_BOOK_PRIMARY_PROVIDER` picks the default primary; an optional
per-symbol `ORDER_BOOK_SYMBOL_OVERRIDES` (JSON) pins symbols to a named provider. When the
**active** provider records ≥ `FAILOVER_ERROR_THRESHOLD` **consecutive** errors within
`FAILOVER_WINDOW_SECONDS` (sliding window) **and a secondary exists**, the router migrates every
active **non-overridden** subscription to the secondary. Errors from a non-active provider are
logged but never trigger a switch. **No auto-failback (v1)** — flapping protection beats it; a
restart restores the primary (deferred to §K). Consumers never notice — the normalized book is
identical from either source.

### Structured log events (grep targets)

| Event | When |
|---|---|
| `order_book.provider_switch` | failover fired (`from=`, `to=`, `symbols=`, `errors=`) |
| `order_book.provider_error` | any provider error recorded (`source=`, `reason=`, `count=`) |
| `order_book.subscriber_lagged` | a cache subscriber queue overflowed (drop-oldest) |
| `order_book.liberator_reconnect` | Liberator reconnect attempt (`attempt=`, `delay=`) |
| `order_book.liberator_ticket` / `_session_error` / `_live_join_failed` / `_parse_skip` | Liberator session lifecycle / parse |
| `order_book.cache_evict` | LRU eviction (DEBUG) |
| `order_book.subscribers` | per-key subscriber refcount changed |

## Cache discipline (D17)

`order_book/service.py` — an in-memory LRU keyed `(symbol, market)`:

- **`get(symbol, market)`** is a **pure, fresh-only read** that **never auto-subscribes**: a
  stale entry (age > `CACHE_MAX_AGE_SECONDS`, default 5 s) reads as **absent**. This is what the
  REST snapshot and the `SimAdapter` call — the sim is never fed a stale book.
- **LRU** bounded by `CACHE_MAX_SYMBOLS` (default 500); least-recently used **by read or write**
  is evicted.
- **`subscription(symbol, market)`** is a refcounted async context manager yielding a bounded
  queue; **0→1 ⇒ `router.subscribe`**, **1→0 ⇒ `router.unsubscribe`**, so the router only talks
  to a venue while at least one consumer wants the symbol. `stream()` is a thin wrapper. The SSE
  route reads the queue with `asyncio.wait_for` so a keep-alive timeout cancels a plain
  `queue.get()` (safe + re-callable) — never an async generator wrapped in `wait_for`.
- Fan-out is lossy-tolerant: a full subscriber queue drops its oldest book + logs
  `subscriber_lagged`, never blocks.

## SimAdapter integration (D21)

`SimAdapter` takes an optional `FillPriceSource` Protocol param (it imports **no** `order_book` —
the dependency arrow points one way). `SimFillPricer` (`adapters/sim_pricing.py`) resolves a
paper-fill price in three hops, each logged so the active source is visible:

1. **Order-book cache** (pure `get`, never subscribes): BUY → best **ask**, SELL → best **bid**,
   **bounded by the order's own limit** (a fill never crosses its limit — BUY at `min(price,
   limit)`, SELL at `max(price, limit)`; a market order is unbounded).
2. **Market-data engine last close** — only when `MARKET_DATA_BASE_URL` is set: the `GET /ohlcv`
   `SET:`/`TFEX:`-prefixed last `1d` bar's `close` (2 s timeout, `MARKET_DATA_API_KEY` SecretStr
   never logged), also limit-bounded; any failure falls through.
3. **`None`** ⇒ the `SimAdapter` uses its own `_reference_price` (limit / stop / configured
   default).

**Price-only:** the `sim_fills`/`sim_reject` fill-plan + FOK/IOC semantics are untouched, and
**with no source injected the adapter is the bit-for-bit Phase-2 deterministic pure function** —
existing tests and the frozen acceptance behaviour are preserved.

## Endpoints (D24 — public-mode readable)

```
GET /order-book/{symbol}          ?market=SET|TFEX   # snapshot; market omitted ⇒ probe SET then TFEX
                                                     # → 200 normalized book | 404 cold/disabled
GET /order-book/{symbol}/stream   ?market=SET|TFEX   # text/event-stream, snapshot-then-updates,
                                                     # keep-alive comment every STREAM_KEEPALIVE_SECONDS
```

api-key-gated but **public-mode readable** — they carry no order data, credential, or raw broker
payload. `/health` gains an additive `order_book` block (active provider, providers, cached
symbols, subscriber count) when the service is on (`null` when off).

## Config (`EXECUTION_ENGINE_` prefix)

| Setting | Default | Effect |
|---|---|---|
| `ORDER_BOOK_ENABLED` | `false` | Master switch (D24) — off keeps the engine bit-for-bit unchanged. Enabling also needs the Liberator provider configured (Liberator api-key). |
| `ORDER_BOOK_PRIMARY_PROVIDER` | `liberator` | `liberator` (the sole provider since the 2026-07-18 Settrade removal) |
| `ORDER_BOOK_SYMBOL_OVERRIDES` | `{}` | JSON map symbol → provider |
| `ORDER_BOOK_FAILOVER_ERROR_THRESHOLD` | `3` | consecutive errors before switch |
| `ORDER_BOOK_FAILOVER_WINDOW_SECONDS` | `30` | error-counting window |
| `ORDER_BOOK_CACHE_MAX_AGE_SECONDS` | `5` | snapshot freshness bound (stale reads as absent) |
| `ORDER_BOOK_CACHE_MAX_SYMBOLS` | `500` | LRU capacity |
| `MARKET_DATA_BASE_URL` | unset | `SimAdapter` fallback hop 2 (last close) |
| `MARKET_DATA_API_KEY` | unset | SecretStr; never logged |
| `STREAM_SUBSCRIBER_QUEUE_SIZE` | `256` | shared per-subscriber back-pressure bound |
| `STREAM_KEEPALIVE_SECONDS` | `15` | SSE keep-alive comment interval |

New deps: `websockets`, `certifi` (explicit — the Liberator WS TLS context builds on it). (The
`settrade-v2` SDK dep was removed with the Settrade provider on 2026-07-18.)

## Real-venue validation (2026-06-12, read-only; AOT = SET, S50M26 = TFEX)

- **Liberator: VERIFIED LIVE.** Both symbols streamed normalized books during the morning
  session (AOT 10×10 depth, S50M26 93 updates in 25 s), Decimal-exact. Three wire findings
  fixed during validation:
  1. **The venue WS host serves an INCOMPLETE TLS chain** (leaf `*.liberator.co.th` only;
     `unable to verify the first certificate`). Fixed by completing the chain client-side:
     the PUBLIC GlobalSign RSA OV SSL CA 2018 intermediate (sha256
     `B6:76:FF:A3:…:76:4A`, expires 2028-11-21, chains to GlobalSign Root R3 in certifi) is
     bundled as `providers/liberator_ca_chain.pem` and loaded into the `wss://` context via
     `build_ssl_context()`. **Verification is never disabled.** Operator override for a
     venue chain rotation: `EXECUTION_ENGINE_ORDER_BOOK_LIBERATOR_EXTRA_CA_PEM`.
  2. The ws-ticket needs no fresh OTP while the upstream session is live
     (`/ws-ticket/health` → `ws_token_available: true`); a dead session is an operator
     OTP-runbook matter (order-routing-safety.md), not a provider concern.
- **Settrade: REMOVED 2026-07-18.** The Settrade SDK-realtime provider (and its 2026-06-12
  validation notes) were removed with broker-023 / `settrade_v2`; the order book is now
  Liberator-only. See [`decision-log.md`](decision-log.md) → the 2026-07-18 removal entry.

## Deferred (§K)

Durable book persistence, market-impact modelling, depth > 10, auto-failback to primary, a
4h/derived-feed story — all out of scope for the execution plane until a concrete consumer exists
(D1/D17 discipline).

## Cross-references

- Decisions D14–D24 → [`decision-log.md`](decision-log.md#phase-5-realisation-decisions-d14d24-2026-06-12)
- Order-update stream (the other Phase-5 SSE surface) → [`order-update-stream.md`](order-update-stream.md)
- Capability matrix (order-update stream row) → [`capability-matrix.md`](capability-matrix.md)
- Phase plan → [`../../docs/plans/phase5-strategy-execution-path-order-streaming.md`](../../docs/plans/phase5-strategy-execution-path-order-streaming.md)

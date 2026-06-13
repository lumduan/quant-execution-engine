# API — `GET /order-book/{symbol}` and `/order-book/{symbol}/stream`

A normalized, **read-only** L2 order book — a snapshot and an SSE feed. Public-mode readable
(api-key-gated). **Default-off** (`ORDER_BOOK_ENABLED=false`). This is the sanctioned read-only
carve-out on the D1 boundary: the engine *reads* a market-data feed but never persists a tick.

| | |
|---|---|
| Method / path | `GET /order-book/{symbol}` · `GET /order-book/{symbol}/stream` |
| Gateway-proxied | `GET /api/v2/engines/execution/order-book/{symbol}[/stream]` (SSE unbuffered) |
| Auth | `X-API-Key` (public-mode readable) |
| Source | `src/quant_execution_engine/api/streams.py::order_book_snapshot`, `::order_book_stream` |

## Parameters

| Param | Where | Type | Required | Effect |
|-------|-------|------|:---:|--------|
| `symbol` | path | string | ✅ | e.g. `PTT`, `S50Z2026` |
| `market` | query | `SET` \| `TFEX` | snapshot: optional · **stream: required** | Snapshot probes `SET` then `TFEX` when omitted; the stream must name one |

## `GET /order-book/{symbol}` — snapshot

```bash
curl "http://quant-api-gateway:8000/api/v2/engines/execution/order-book/PTT?market=SET" \
  -H "X-API-Key: <your-api-key>"
```

```json
{
  "symbol": "PTT",
  "market": "SET",
  "bid_levels": [ { "price": "35.50", "volume": 1200 }, { "price": "35.25", "volume": 800 } ],
  "ask_levels": [ { "price": "35.75", "volume": 1500 }, { "price": "36.00", "volume": 600 } ],
  "bid_flag": "NORMAL",
  "ask_flag": "NORMAL",
  "sequence": 880412,
  "source": "liberator",
  "received_at": "2026-06-13T09:00:00Z"
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `symbol` / `market` | string | the book's key |
| `bid_levels` / `ask_levels` | array | depth levels; `[0]` is the best bid / ask |
| `…price` | string (Decimal) | level price |
| `…volume` | int | level volume (`>= 0`) |
| `bid_flag` / `ask_flag` | string | venue state flag (`"NORMAL"` by default) |
| `sequence` | int | the provider's event sequence |
| `source` | string | the providing feed: `settrade` \| `liberator` |
| `received_at` | string (UTC) | when the engine received this book |

## `GET /order-book/{symbol}/stream` — SSE

Snapshot-then-updates, with `: keep-alive` comments while idle. Each `data:` frame is a full
normalized `OrderBook` (same shape as the snapshot).

```bash
curl -N \
  "http://quant-api-gateway:8000/api/v2/engines/execution/order-book/PTT/stream?market=SET" \
  -H "X-API-Key: <your-api-key>"
```

```
data: {"symbol":"PTT","market":"SET","bid_levels":[{"price":"35.50","volume":1200}],"ask_levels":[{"price":"35.75","volume":1500}],"bid_flag":"NORMAL","ask_flag":"NORMAL","sequence":880412,"source":"liberator","received_at":"2026-06-13T09:00:00Z"}

: keep-alive
```

## Dual-provider + failover

The book is fed by **two providers** — Settrade realtime (the `settrade-v2` SDK, contained behind a
lazy import + `asyncio.to_thread`; the E21 order-routing SDK ban is unchanged) and Liberator
WebSocket (ws-ticket + raw `websockets` Engine.IO v4; no `curl_cffi`). The primary is
`ORDER_BOOK_PRIMARY_PROVIDER` (default `liberator`). On `ORDER_BOOK_FAILOVER_ERROR_THRESHOLD`
consecutive errors within `ORDER_BOOK_FAILOVER_WINDOW_SECONDS` the active provider fails over (no
auto-failback in v1). Per-symbol overrides: `ORDER_BOOK_SYMBOL_OVERRIDES`.

## The D1 boundary

This is **read-only market data**: no durable storage, no order data, no credential crosses these
routes. A dropped tick is a resubscribe — never a replay from a store. The book also feeds
`SimAdapter` fill pricing (best bid/offer hop). See
[`../architecture/overview.md`](../architecture/overview.md#the-two-planes-d1).

## Errors

| Code | HTTP | When |
|------|:---:|------|
| `order_book_unavailable` | 404 | the service is **disabled** (`ORDER_BOOK_ENABLED=false`) **or** no fresh book is cached for the symbol (cold) |

## Notes

- `ORDER_BOOK_ENABLED=false` is the default, so both routes return `404 order_book_unavailable` until
  an operator enables the service **and** configures at least one provider. Enabling Settrade realtime
  also requires the venue-side portal flag (else `DISPATCH-UM-04 "User is inactive"`). See
  [`../operations/configuration.md`](../operations/configuration.md) and
  [`../operations/troubleshooting.md`](../operations/troubleshooting.md).

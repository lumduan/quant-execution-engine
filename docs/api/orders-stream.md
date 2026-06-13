# API — `GET /orders/stream`

Server-Sent Events (SSE) stream of normalized order-update events. Public-mode readable
(api-key-gated) — a strategy subscribes and reacts to fills without owner mode. **The stream is
advisory; the durable `execution.orders` store is the source of truth.**

| | |
|---|---|
| Method / path | `GET /orders/stream` |
| Gateway-proxied | `GET /api/v2/engines/execution/orders/stream` (unbuffered pass-through) |
| Auth | `X-API-Key` (public-mode readable) |
| Source | `src/quant_execution_engine/api/streams.py::order_stream` |

## Query parameters

| Param | Type | Required | Effect |
|-------|------|:---:|--------|
| `strategy_id` | string | | Only events for orders stamped with this `X-Strategy-Id`. **DB-seeded** at subscribe time so orders submitted before a restart still match |
| `client_order_id` | string | | Only events for this order |
| `last_event_id` | int | | Reconnect cursor (query fallback for the `Last-Event-ID` header) |

Filters compose **conjunctively**. The standard `Last-Event-ID` request header wins over the query
param; an unparseable cursor is a `422` (a typo must be visible, never a silent reset).

## SSE protocol

Each frame is one of:

```
id: 42
event: NEW
data: {"seq":42,"client_order_id":"…","strategy_id":"csm-set","engine_state":"NEW",...}
```

- `id:` = the monotonic `seq` (use it as your reconnect cursor).
- `event:` = the **engine-state** string (the internal 9-state value, a strict superset of the public
  status), or one of the advisory event types `gap` / `resync_required`.
- `data:` = the JSON `OrderUpdateEvent` (Decimal-as-string, UTC timestamps).

Advisory frames:

| Frame | Meaning |
|-------|---------|
| `: keep-alive` (SSE comment) | emitted every `STREAM_KEEPALIVE_SECONDS` (default 15) while idle |
| `event: gap` / `data: {"dropped": N}` | this subscriber fell behind and `N` events were dropped from its queue (back-pressure) |
| `event: resync_required` / `data: {"after_seq": N}` | the reconnect cursor has fallen off the ring buffer — re-read the store, then continue from live |

Reconnect replays the ring buffer (`STREAM_RING_BUFFER_SIZE`, default 1024) after the cursor; a
cursor older than the ring yields one `resync_required`.

## `OrderUpdateEvent` schema

| Field | Type | Meaning |
|-------|------|---------|
| `seq` | int | monotonic sequence (= the SSE `id:`) |
| `client_order_id` | string | the order |
| `strategy_id` | string \| null | from `X-Strategy-Id` (D16) or DB-seeded |
| `engine_state` | string | internal 9-state value (= the SSE `event:`) |
| `status` | string | public 6-value status |
| `broker_order_id` | string \| null | venue id once acked |
| `price` | string (Decimal) \| null | populated on replace/amend events |
| `quantity` | int \| null | populated on replace/amend events |
| `fill` | object \| null | present only on `PARTIALLY_FILLED` / `FILLED` transitions |
| `fill.broker_fill_id` | string | venue fill id |
| `fill.price` | string (Decimal) | fill price |
| `fill.quantity` | int | fill quantity |
| `fill.exec_ts` | string (UTC) | venue execution time |
| `ts` | string (UTC) | event time |

## Strategy attribution (`X-Strategy-Id`)

`POST /orders` accepts an optional `X-Strategy-Id` header (D16) which is persisted to
`execution.orders.strategy_id` and echoed on every event for that order. `strategy_id` stream
filtering uses it — and because it is DB-seeded at subscribe time, a reconnect after a restart still
matches the strategy's pre-restart orders.

## Example — SSE session

```bash
curl -N \
  "http://quant-api-gateway:8000/api/v2/engines/execution/orders/stream?strategy_id=csm-set" \
  -H "X-API-Key: <your-api-key>"
```

```
id: 41
event: PENDING_NEW
data: {"seq":41,"client_order_id":"7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34","strategy_id":"csm-set","engine_state":"PENDING_NEW","status":"NEW","broker_order_id":null,"price":null,"quantity":null,"fill":null,"ts":"2026-06-13T09:00:00Z"}

id: 42
event: NEW
data: {"seq":42,"client_order_id":"7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34","strategy_id":"csm-set","engine_state":"NEW","status":"NEW","broker_order_id":"SIM-1A2B3C","price":null,"quantity":null,"fill":null,"ts":"2026-06-13T09:00:00Z"}

id: 43
event: FILLED
data: {"seq":43,"client_order_id":"7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34","strategy_id":"csm-set","engine_state":"FILLED","status":"FILLED","broker_order_id":"SIM-1A2B3C","price":null,"quantity":null,"fill":{"broker_fill_id":"SIMF-1A2B3C-0","price":"35.50","quantity":100,"exec_ts":"2026-06-13T09:00:00Z"},"ts":"2026-06-13T09:00:00Z"}

: keep-alive
```

## Errors

| Code | HTTP | When |
|------|:---:|------|
| `order_stream_unavailable` | 503 | the event hub is not running (lifespan not started) |
| (invalid `Last-Event-ID`) | 422 | the reconnect cursor is unparseable |

## Notes

- The hub publishes **post-success, non-blocking, exception-proof** from the five repository writers
  every transition funnels through — a slow or dead subscriber never blocks an order write. If you
  miss events, the durable store and the per-order audit are authoritative.
- Reconcile residuals on reconnect with [`orders-get.md`](orders-get.md) (`GET /orders/{cid}`).

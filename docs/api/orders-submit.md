# API — `POST /orders`

Submit a `NormalizedOrder`. **Owner-mode only.** Idempotent on `client_order_id`. The engine runs
the full submit pipeline (kill-switch → idempotency dedupe → capability gate → PTRM risk gate →
price-band → stage ladder → adapter) and persists the lifecycle before anything reaches a venue.

| | |
|---|---|
| Method / path | `POST /orders` |
| Gateway-proxied | `POST /api/v2/engines/execution/orders` |
| Auth | owner mode + `X-API-Key` |
| Headers | `X-Strategy-Id` (optional, D16) · `X-API-Key` |
| Source | `src/quant_execution_engine/api/routes.py::submit_order` |

## Request body — `NormalizedOrder`

| Field | Type | Required | Allowed / constraint | Notes |
|-------|------|:---:|----------------------|-------|
| `client_order_id` | string | ✅ | **UUIDv4** | The idempotency key; client-generated, format-validated |
| `broker` | string | ✅ | `sim` \| `liberator` \| `settrade` | Routing target (subject to the stage ladder) |
| `account` | string | ✅ | non-empty | Broker account; never logged |
| `market` | string | ✅ | `SET` \| `TFEX` | |
| `symbol` | string | ✅ | non-empty | e.g. `PTT`, `S50Z2026` |
| `side` | string | ✅ | `BUY` \| `SELL` | |
| `order_type` | string | ✅ | `MARKET` \| `LIMIT` \| `STOP` \| `STOP_LIMIT` \| `ICEBERG` \| `MTL` \| `ATO` \| `ATC` | Must be supported by the `(broker, market)` cell |
| `price` | string (Decimal) | conditional | `> 0`, never a float | **Required** for `LIMIT` / `STOP_LIMIT` |
| `stop_price` | string (Decimal) | conditional | `> 0`, never a float | **Required** for `STOP` / `STOP_LIMIT` |
| `quantity` | int | ✅ | `> 0` | |
| `display_qty` | int | conditional | `> 0`, `<= quantity` | **Required iff** `order_type=ICEBERG`; forbidden otherwise |
| `tif` | string | ✅ | `DAY` \| `IOC` \| `FOK` \| `GTC` | |
| `position_effect` | string | conditional | `OPEN` \| `CLOSE` | **Required for TFEX**; must be **omitted for SET** |
| `metadata` | object | | default `{}` | Free-form; the `SimAdapter` reads `sim_fills` / `sim_reject` here |

`extra="forbid"` — unknown fields are a 422. Prices are **Decimal-as-string** (`"35.50"`); a JSON
float is rejected outright.

## Response

- `201 Created` — first accept.
- `200 OK` — idempotent resend of a known `client_order_id` (the **prior** result; no second broker
  call).

Body is a `NormalizedOrderResult`:

| Field | Type | Meaning |
|-------|------|---------|
| `client_order_id` | string | echo |
| `broker_order_id` | string \| null | venue id; `null` until the ack |
| `broker` | string | routed broker |
| `status` | string | public status: `NEW` \| `PARTIALLY_FILLED` \| `FILLED` \| `CANCELLED` \| `REJECTED` \| `EXPIRED` |
| `engine_state` | string | truthful internal 9-state value (incl. `PENDING_*`) |
| `filled_qty` | int | cumulative filled |
| `remaining_qty` | int | `quantity − filled_qty` |
| `avg_fill_price` | string (Decimal) \| null | volume-weighted average fill price |
| `reject_reason` | string \| null | venue/internal reason when `REJECTED` |
| `created_at` / `updated_at` | string (UTC) | e.g. `"2026-06-13T09:00:00Z"` |

## Errors (typed envelope)

`{"error": {"code": "...", "message": "...", "client_order_id": "...", "detail": {...}}}`

| Code | HTTP | When |
|------|:---:|------|
| `public_mode` | 403 | engine is in public mode (submission disabled) |
| `stage_rejected` | 403 | `live` stage (gated) — or a stage that cannot route this broker |
| `kill_switch_engaged` | 503 | the kill-switch is engaged (checked first) |
| `capability_unsupported` | 422 | unsupported `(broker, market, order_type, tif, position_effect)` |
| `risk_rejected` | 422 | a PTRM cap (`detail.cap` = `value`/`qty`/`notional`); **429** when `cap=rate_limit` |
| `price_band_exceeded` | 422 | LIMIT price too far off last close (price-band enabled) |
| `duplicate_burst_detected` | 409 | same economic fingerprint under a **different** cid within the window |
| `broker_circuit_open` | 503 | the target broker's circuit breaker is open |
| `submit_in_flight` | 409 | an identical `client_order_id` is mid-flight with no durable row yet |

## Idempotency & stage gating

- **Idempotency:** re-POSTing the same `client_order_id` returns `200` with the prior result — never
  a second venue order. Use a fresh UUIDv4 per logical order; reuse the id only on a transport/5xx
  retry.
- **Stage gating:** `sim` (default) and `paper` route to `SimAdapter`; `micro_live` routes the real
  venue at PTRM-capped size; `live` is gated (`stage_rejected`). See
  [`../architecture/overview.md`](../architecture/overview.md).

## Example — SET equity LIMIT BUY via `SimAdapter`

```bash
curl -X POST http://quant-api-gateway:8000/api/v2/engines/execution/orders \
  -H "X-API-Key: <your-api-key>" \
  -H "X-Strategy-Id: csm-set" \
  -H "Content-Type: application/json" \
  -d '{
    "client_order_id": "7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34",
    "broker": "sim",
    "account": "<your-account>",
    "market": "SET",
    "symbol": "PTT",
    "side": "BUY",
    "order_type": "LIMIT",
    "price": "35.50",
    "quantity": 100,
    "tif": "DAY"
  }'
```

```json
{
  "client_order_id": "7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34",
  "broker_order_id": "SIM-1A2B3C",
  "broker": "sim",
  "status": "FILLED",
  "engine_state": "FILLED",
  "filled_qty": 100,
  "remaining_qty": 0,
  "avg_fill_price": "35.50",
  "reject_reason": null,
  "created_at": "2026-06-13T09:00:00Z",
  "updated_at": "2026-06-13T09:00:00Z"
}
```

> `position_effect` is omitted (SET). For a TFEX order it is **required**, e.g.
> `"market": "TFEX", "symbol": "S50Z2026", "position_effect": "OPEN"`.

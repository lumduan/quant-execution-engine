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
| `broker` | string | ✅ | `sim` \| `liberator` \| `streaming_pro` | Routing target (subject to the stage ladder). ⚠️ `settrade` (broker-023, the Settrade Open API) was **removed 2026-07-18** — the third broker is `streaming_pro` |
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
| `broker_order_id` | string \| null | venue id. ⚠️ **Not always on the ack** — the Liberator place-ack carries none at all, so it is recovered from venue truth during the submit (see `resolution` below). `null` means it was **not** recovered |
| `broker` | string | routed broker |
| `status` | string | public status: `NEW` \| `PARTIALLY_FILLED` \| `FILLED` \| `CANCELLED` \| `REJECTED` \| `EXPIRED` |
| `engine_state` | string | truthful internal 9-state value (incl. `PENDING_*`) |
| `filled_qty` | int | cumulative filled |
| `remaining_qty` | int | `quantity − filled_qty` |
| `avg_fill_price` | string (Decimal) \| null | volume-weighted average fill price |
| `reject_reason` | string \| null | venue/internal reason when `REJECTED` |
| `created_at` / `updated_at` | string (UTC) | e.g. `"2026-06-13T09:00:00Z"` |

…plus one field that is **not** part of `NormalizedOrderResult`:

| Field | Type | Meaning |
|-------|------|---------|
| `resolution` | string | `confirmed` \| `pending` \| `unknown` — how much the engine actually **knows**, at submit time |

### `resolution` — read this before writing an unwind path (TK-0423)

It answers what the order state cannot: *did we read the venue, or are we guessing?*
Some venues (Liberator) return **no order handle on the place-ack at all**, so after placing,
the engine bursts against venue truth — cadence `HANDLE_RECOVERY_CADENCE_MS` (250 ms, ≈ the read
latency), at least `HANDLE_RECOVERY_MIN_POLLS` (3) attempts, bounded by
`HANDLE_RECOVERY_DEADLINE_MS` (1500 ms from **submit**) — which may only end the burst *after*
the floor is met, so a slow placement can never starve the retries into a false `unknown`.

| value | meaning | may the caller resubmit? |
|---|---|---|
| `confirmed` | the venue was read and answered; `broker_order_id` and state are venue truth | n/a |
| `pending` | the venue **was** read; the order is accepted and working, not yet resolvable there | **no** — it is working |
| `unknown` | the venue could **not** be read within the budget | 🔴 **never** — the order may be LIVE with its handle unrecovered; a resubmit double-fills |

🔴 **`pending` and `unknown` must not be collapsed.** They are the same "we did not get the handle"
outcome arriving by two different routes, and only one of them is dangerous. Read `unknown` as
*"an order may exist that I cannot name"*: poll `GET /orders/{client_order_id}` or let the
reconcile loop finish the job — never re-place.

**The guarantee is structural, not statistical:** this endpoint cannot return before the venue has
been read at least once, so submit-to-known is bounded by the call's own latency (~1.1–1.5 s
measured) rather than by the 12 s reconcile interval.

⚠️ `GET /orders/{client_order_id}` deliberately does **not** carry `resolution` — a later read is
not evidence about what was known at submit time.

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

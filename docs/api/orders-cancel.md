# API — `DELETE /orders/{client_order_id}`

Cancel a resting order. **Owner-mode only.** Deliberately **not** kill-switch-gated — a cancel
reduces risk, and the kill-switch mass-cancel sweep uses this same path.

| | |
|---|---|
| Method / path | `DELETE /orders/{client_order_id}` |
| Gateway-proxied | `DELETE /api/v2/engines/execution/orders/{client_order_id}` |
| Auth | owner mode + `X-API-Key` |
| Source | `src/quant_execution_engine/api/routes.py::cancel_order` |

## Path parameter

| Param | Type | Notes |
|-------|------|-------|
| `client_order_id` | string (UUIDv4) | the resting order to cancel |

## Behaviour

The cancel walks the frozen two-step edge — `NEW` / `PARTIALLY_FILLED → PENDING_CANCEL → CANCELLED`:

- It **proceeds from `PARTIALLY_FILLED`** (the filled quantity stands; the resting remainder is
  pulled).
- `PENDING_CANCEL` during the network gap between the venue request and its ack; a re-cancel of an
  order already in `PENDING_CANCEL` is **idempotent** (returns the current state).
- A cancel from a **transient** state (`PENDING_NEW`, `PENDING_REPLACE`) is rejected
  (`illegal_transition`) — there is no frozen cancel edge from those; they resolve via the
  reconciliation loop first.
- A cancel of a **terminal** order (`FILLED`/`CANCELLED`/`REJECTED`/`EXPIRED`) is `illegal_transition`.

**Measured (Liberator, 2026-08-25, a genuinely resting SET order):** `DELETE` returned **200 in
1,027 ms**, engine terminal **`CANCELLED` at +1,032 ms**.

### A cancel records `CANCELLED` — whoever issued it

The Liberator venue reports a cancelled order as `statusShow` `C`, `X` **or** `XC` — its own
dictionary gives all three the label *Cancelled*, and the venue has **no expiry code at all**. The
engine maps all three to `CANCELLED`, so an unwind path asking *"did my cancel work?"* gets the same
answer whether the cancel came from this endpoint, from the bridge, or from the venue.

⚠️ **This was wrong until 2026-08-25 ([[TK-0428]]).** `classify_venue_state` mapped `X` → `EXPIRED`,
so a cancel discovered by the reconciler was recorded as *expired*. One historical row
(order `18439`) still carries the misclassification and is deliberately left — `EXPIRED → CANCELLED`
is not one of the frozen 13 edges.

Venue wire detail: [`../../../docs/reference/liberator-order-wire.md`](../../../docs/reference/liberator-order-wire.md) §3
and the authoritative dictionary in
[`../../../docs/reference/liberator-order-plane.md`](../../../docs/reference/liberator-order-plane.md).

At the venue: Liberator cancels by orderNo list (bulk ≤ 50) + PIN. (Settrade's `PATCH .../cancel` left
with broker-023 on 2026-07-18.)

## Request

```bash
curl -X DELETE \
  http://quant-api-gateway:8000/api/v2/engines/execution/orders/7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34 \
  -H "X-API-Key: <your-api-key>"
```

## Response `200 OK` — `NormalizedOrderResult`

```json
{
  "client_order_id": "7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34",
  "broker_order_id": "SIM-1A2B3C",
  "broker": "sim",
  "status": "CANCELLED",
  "engine_state": "CANCELLED",
  "filled_qty": 0,
  "remaining_qty": 100,
  "avg_fill_price": null,
  "reject_reason": null,
  "created_at": "2026-06-13T09:00:00Z",
  "updated_at": "2026-06-13T09:01:10Z"
}
```

## Errors

| Code | HTTP | When |
|------|:---:|------|
| `public_mode` | 403 | engine is in public mode |
| `order_not_found` | 404 | unknown `client_order_id` |
| `illegal_transition` | 409 | order is terminal, or in a transient `PENDING_NEW`/`PENDING_REPLACE` state |
| `broker_circuit_open` | 503 | the broker's circuit breaker is open |

## Notes

- A cancel that the venue does not confirm leaves the order in `PENDING_CANCEL`; the reconciliation
  loop drives it to `CANCELLED` (never silently). See
  [`../operations/troubleshooting.md`](../operations/troubleshooting.md).
- The kill-switch does **not** block this endpoint (unlike `PATCH`/`POST`). See
  [`../operations/kill-switch.md`](../operations/kill-switch.md).

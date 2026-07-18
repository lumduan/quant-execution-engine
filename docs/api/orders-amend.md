# API — `PATCH /orders/{client_order_id}`

Amend a resting order's price and/or quantity. **Owner-mode only.** The engine branches on the
order's declared amend semantics (`GET /capabilities` → the cell's `amend` field): **native**
amends in place and keeps the same `client_order_id`; **cancel_replace** cancels the old order and
submits a replacement under a **new** `client_order_id`.

> **2026-07-18:** the Settrade Open API (broker-023) was removed. Among current brokers, **only the
> `sim` simulator declares `native` amend**; the real brokers **Liberator + Streaming Pro** both use
> **cancel_replace**. The route and both code paths are unchanged.

| | |
|---|---|
| Method / path | `PATCH /orders/{client_order_id}` |
| Gateway-proxied | `PATCH /api/v2/engines/execution/orders/{client_order_id}` |
| Auth | owner mode + `X-API-Key` |
| Source | `src/quant_execution_engine/api/routes.py::amend_order` |

## Request body — `AmendOrderRequest`

| Field | Type | Required | Notes |
|-------|------|:---:|-------|
| `new_price` | string (Decimal) | conditional | `> 0`, never a float |
| `new_qty` | int | conditional | `> 0`; must exceed already-filled quantity |
| `new_client_order_id` | string (UUIDv4) | conditional | **Required for `cancel_replace`** brokers (Liberator, Streaming Pro); **must be omitted** for `native` brokers (`sim`) |

At least one of `new_price` / `new_qty` is required (422 otherwise). `extra="forbid"`. There is **no**
`new_display_qty` field — an iceberg's display size is not amendable through this route.

## The critical asymmetry

| | `native` (`sim`) | `cancel_replace` (Liberator, Streaming Pro) |
|---|---|---|
| Edge | `NEW → PENDING_REPLACE → NEW` (atomic `replace_order`) | old order `→ PENDING_CANCEL → CANCELLED`; replacement is a fresh `PENDING_NEW` |
| `client_order_id` | **same** (omit `new_client_order_id`) | **new** (supply `new_client_order_id`) |
| Atomicity | atomic at the engine | **non-atomic**: a brief no-resting-order window + queue-priority loss |
| On venue reject | **non-terminal** restore `PENDING_REPLACE → NEW`; typed `amend_rejected` (409); order stays live, `reject_reason` not written (sim never rejects an amend, so this path is retained but not exercised by a live broker) | the old order is already cancelled; the replacement follows normal submit rules |

The declared `cancel_replace` consequences (queue-priority loss, the no-resting-order gap) are why
callers must read `GET /capabilities` and never assume amend is atomic.

## Gating & re-checks

- **Kill-switch first.** Unlike `DELETE`, the amend path is kill-switch-gated up front — an amend can
  *increase* exposure, so it must not slip a larger order past an engaged switch.
- **PTRM re-check, no exemption.** The amended order re-runs the risk gate; a price-only amend that
  reuses the same quantity inside the duplicate-burst window can be risk-rejected.

## Request — native amend (`sim`, price change only)

```bash
curl -X PATCH \
  http://quant-api-gateway:8000/api/v2/engines/execution/orders/7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34 \
  -H "X-API-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{ "new_price": "35.75" }'
```

```json
{
  "client_order_id": "7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34",
  "broker_order_id": "SIM-7c2f4e9a",
  "broker": "sim",
  "status": "NEW",
  "engine_state": "NEW",
  "filled_qty": 0,
  "remaining_qty": 100,
  "avg_fill_price": null,
  "reject_reason": null,
  "created_at": "2026-06-13T09:00:00Z",
  "updated_at": "2026-06-13T09:02:00Z"
}
```

> A Liberator / Streaming Pro `cancel_replace` amend instead supplies `"new_client_order_id"` and the
> response carries that **new** id — the old order is `CANCELLED`. (Real Liberator / Streaming Pro
> amends require `micro_live` + owner mode + configured broker creds; `live` is gated.)

## Errors

| Code | HTTP | When |
|------|:---:|------|
| `public_mode` | 403 | engine is in public mode |
| `kill_switch_engaged` | 503 | the kill-switch is engaged (amends are gated) |
| `order_not_found` | 404 | unknown `client_order_id` |
| `amend_rejected` | 409 | no change supplied, venue rejected the amend (native; order stays live), or a `cancel_replace` amend missing `new_client_order_id` |
| `illegal_transition` | 409 | order is not in an amendable state (e.g. already terminal or mid-amend) |
| `risk_rejected` | 422 | the amended order fails a PTRM cap |
| `capability_unsupported` | 422 | the amended shape is unsupported for the cell |
| `broker_circuit_open` | 503 | the broker's circuit breaker is open |

## Notes

- Native amend is `sim`-only and synchronous in-request, so there is no stranded `PENDING_REPLACE`
  window in practice. (The Settrade reconciler's `replace_resolve` action that repaired a stranded
  native amend was removed with broker-023 on 2026-07-18.) The `cancel_replace` real-broker path
  has no `PENDING_REPLACE` state. See [`../operations/troubleshooting.md`](../operations/troubleshooting.md).

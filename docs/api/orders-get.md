# API — `GET /orders/{client_order_id}`

Read one order's normalized state. Public-mode readable (api-key-gated) — a strategy can poll its own
orders without owner mode.

| | |
|---|---|
| Method / path | `GET /orders/{client_order_id}` |
| Gateway-proxied | `GET /api/v2/engines/execution/orders/{client_order_id}` |
| Auth | `X-API-Key` (public-mode readable) |
| Source | `src/quant_execution_engine/api/routes.py::get_order` |

## Path parameter

| Param | Type | Notes |
|-------|------|-------|
| `client_order_id` | string (UUIDv4) | the id supplied at submit |

## Request

```bash
curl http://quant-api-gateway:8000/api/v2/engines/execution/orders/7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34 \
  -H "X-API-Key: <your-api-key>"
```

## Response `200 OK` — `NormalizedOrderResult`

```json
{
  "client_order_id": "7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34",
  "broker_order_id": "SIM-1A2B3C",
  "broker": "sim",
  "status": "PARTIALLY_FILLED",
  "engine_state": "PARTIALLY_FILLED",
  "filled_qty": 60,
  "remaining_qty": 40,
  "avg_fill_price": "35.50",
  "reject_reason": null,
  "created_at": "2026-06-13T09:00:00Z",
  "updated_at": "2026-06-13T09:00:05Z"
}
```

Field meanings are identical to the [`POST /orders`](orders-submit.md) result. Note `status` is the
frozen 6-value public enum while `engine_state` carries the truthful internal 9-state value (so a
`PENDING_CANCEL` order reads `status: "NEW"` / `engine_state: "PENDING_CANCEL"`).

## Errors

| Code | HTTP | When |
|------|:---:|------|
| `order_not_found` | 404 | unknown `client_order_id` |

## Notes

- This is a point read of the durable store. For a live push of every transition, subscribe to
  [`orders-stream.md`](orders-stream.md) instead of polling.
- For the full transition history of one order, use
  [`admin.md`](admin.md) `GET /admin/orders/{cid}/audit` (owner-mode).

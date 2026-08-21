# API — `GET /health`

Liveness/readiness probe. Reports the configured stage, public mode, and — when configured — each
broker's circuit-breaker + session state and the order-book service state. **Not auth-gated.**

| | |
|---|---|
| Method / path | `GET /health` |
| Gateway-proxied | `GET /api/v2/engines/execution/health` |
| Auth | none |
| Source | `src/quant_execution_engine/api/routes.py::health` |

## Request

```bash
# Engine-direct (developer host access)
curl http://localhost:8400/health

# Via the gateway proxy (in-cluster service name)
curl http://quant-api-gateway:8000/api/v2/engines/execution/health
```

## Response `200 OK` — public / sim (broker-free)

```json
{
  "status": "ok",
  "service": "quant-execution-engine",
  "version": "0.6.0",
  "stage": "sim",
  "public_mode": true,
  "brokers": null,
  "order_book": null
}
```

## Response `200 OK` — owner mode, both adapters live, order book on

```json
{
  "status": "ok",
  "service": "quant-execution-engine",
  "version": "0.6.0",
  "stage": "micro_live",
  "public_mode": false,
  "brokers": {
    "liberator": { "breaker_state": "closed", "session_healthy": true },
    "streaming_pro": { "breaker_state": "closed", "session_healthy": true }
  },
  "order_book": {
    "active_provider": "liberator",
    "providers": ["liberator"],
    "cached_symbols": 12,
    "subscribers": 3
  }
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `status` | string | `"ok"` |
| `service` | string | always `"quant-execution-engine"` |
| `version` | string | package version |
| `stage` | string | the safety ladder stage: `sim` \| `paper` \| `micro_live` \| `live` |
| `public_mode` | bool | `true` ⇒ order-submission + admin endpoints disabled |
| `brokers` | object \| null | `null` when broker-free; otherwise one entry per configured broker — `liberator` and/or `streaming_pro` |
| `brokers.<b>.breaker_state` | string | `"closed"` \| `"open"` \| `"half_open"` |
| `brokers.<b>.session_healthy` | bool \| null | last heartbeat result; `null` before the first probe |
| `brokers.<b>.sessions` | object \| null | per-market last heartbeat for a multi-session broker. ⚠️ **No current broker populates it** — always `null`. Retained for a future multi-app broker; it was the Phase-4.1 Settrade (broker-023) field, and that adapter was removed 2026-07-18 |
| `order_book` | object \| null | `null` when `ORDER_BOOK_ENABLED=false`; otherwise the live order-book state |
| `order_book.active_provider` | string | the current failover-active provider |
| `order_book.providers` | array | every configured provider |
| `order_book.cached_symbols` | int | `(symbol, market)` keys currently cached |
| `order_book.subscribers` | int | live SSE subscriber queues |

## Notes

- A `breaker_state` of `"open"` means that broker's circuit breaker has tripped on consecutive
  heartbeat failures — placements to it reject with `broker_circuit_open` (503) and its open orders
  were mass-cancelled. See [`../operations/troubleshooting.md`](../operations/troubleshooting.md).
- `/health` is the only endpoint with no `X-API-Key` requirement (so an orchestrator can probe it).

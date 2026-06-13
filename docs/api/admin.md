# API — Admin (`/admin/*`)

The owner-mode admin surface: the runtime kill-switch and the structured audit reads. **Owner-mode
only** (`require_owner_mode` → `public_mode` 403 in public mode). These routes are **engine-direct**
in spirit — they carry no raw broker payload and never expose a credential.

| | |
|---|---|
| Auth | owner mode + `X-API-Key` |
| Headers | `X-Operator-Id` (optional; default `anonymous`) on engage/disengage |
| Source | `api/routes.py` (kill-switch) · `api/audit.py` (audit) |

---

## `GET /admin/kill-switch` — state

```bash
curl http://localhost:8400/admin/kill-switch -H "X-API-Key: <your-api-key>"
```

```json
{ "engaged": false, "source": null }
```

| Field | Type | Meaning |
|-------|------|---------|
| `engaged` | bool | whether the switch is tripped |
| `source` | string \| null | `"env"` (boot-pinned) \| `"redis"` (runtime trip) \| `null` |

---

## `POST /admin/kill-switch/engage`

Trips the switch: **rejects all new submits + mass-cancels every open order** (flatten-and-halt).
**Idempotent** — a second engage returns `already_engaged=true` and runs **no** second sweep. Emits a
structured `kill_switch.engaged` audit log (operator + counts; never a secret).

```bash
curl -X POST http://localhost:8400/admin/kill-switch/engage \
  -H "X-API-Key: <your-api-key>" \
  -H "X-Operator-Id: ops-alice"
```

```json
{
  "engaged": true,
  "already_engaged": false,
  "cancelled_count": 3,
  "cancelled": ["7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34", "…", "…"],
  "failed": []
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `engaged` | bool | always `true` after this call |
| `already_engaged` | bool | `true` ⇒ it was already tripped; no second sweep ran |
| `cancelled_count` | int | number of orders cancelled (`= len(cancelled)`) |
| `cancelled` / `failed` | array | cids that were / weren't cancelled by the sweep |

`503` if the Redis sidecar (which backs the runtime flag) is unavailable.

---

## `POST /admin/kill-switch/disengage`

Clears the **runtime** trip (the env flag always wins). Emits a `kill_switch.disengaged` audit log.

```bash
curl -X POST http://localhost:8400/admin/kill-switch/disengage \
  -H "X-API-Key: <your-api-key>" \
  -H "X-Operator-Id: ops-alice"
```

```json
{ "engaged": false, "source": null }
```

| Code | HTTP | When |
|------|:---:|------|
| `kill_switch_not_engaged` | 409 | the switch is already clear |
| `kill_switch_env_pinned` | 409 | the switch is pinned on by `EXECUTION_ENGINE_KILL_SWITCH_ENGAGED=true` (clear the env flag + restart) |

---

## `GET /admin/orders/{client_order_id}/audit` — per-order trail

The full synthesized audit trail for one order, derived from the append-only `order_events` rows (no
schema change).

```bash
curl http://localhost:8400/admin/orders/7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34/audit \
  -H "X-API-Key: <your-api-key>"
```

```json
{
  "client_order_id": "7c2f4e9a-1b3d-4a6c-8e5f-0d9b2a1c6e34",
  "broker": "sim",
  "symbol": "PTT",
  "events": [
    { "seq": 1, "from_status": null, "to_status": "PENDING_NEW", "broker_order_id": null, "event_type": "create", "occurred_at": "2026-06-13T09:00:00Z", "metadata": {} },
    { "seq": 2, "from_status": "PENDING_NEW", "to_status": "NEW", "broker_order_id": "SIM-1A2B3C", "event_type": "ack", "occurred_at": "2026-06-13T09:00:00Z", "metadata": { "broker_order_id": "SIM-1A2B3C" } },
    { "seq": 3, "from_status": "NEW", "to_status": "FILLED", "broker_order_id": "SIM-1A2B3C", "event_type": "fill", "occurred_at": "2026-06-13T09:00:00Z", "metadata": { "price": "35.50", "quantity": 100 } }
  ]
}
```

`event_type` is a pure transition mapping: `create` (birth), `ack` (`PENDING_NEW→NEW`), `replace`
(`PENDING_REPLACE→NEW`), `fill` (`→PARTIALLY_FILLED`/`FILLED`), `cancel_request` (`→PENDING_CANCEL`),
`cancel` (`→CANCELLED`), `replace_request` (`→PENDING_REPLACE`), `reject` (`→REJECTED`), `expire`
(`→EXPIRED`). `404 order_not_found` for an unknown cid.

---

## `GET /admin/audit/export` — NDJSON export

Streams every `order_events` row as NDJSON (one JSON object per line) via a server-side cursor — never
buffered. Filters: `from_ts` (inclusive), `to_ts` (exclusive), `strategy_id` (exact).

```bash
curl "http://localhost:8400/admin/audit/export?from_ts=2026-06-13T00:00:00Z&strategy_id=csm-set" \
  -H "X-API-Key: <your-api-key>"
```

```
{"seq":1,"from_status":null,"to_status":"PENDING_NEW","broker_order_id":null,"event_type":"create","occurred_at":"2026-06-13T09:00:00Z","metadata":{}}
{"seq":2,"from_status":"PENDING_NEW","to_status":"NEW","broker_order_id":"SIM-1A2B3C","event_type":"ack","occurred_at":"2026-06-13T09:00:00Z","metadata":{"broker_order_id":"SIM-1A2B3C"}}
```

| Param | Type | Effect |
|-------|------|--------|
| `from_ts` | datetime (UTC) | inclusive lower bound |
| `to_ts` | datetime (UTC) | exclusive upper bound |
| `strategy_id` | string | exact match (joined to `orders`) |

Media type `application/x-ndjson`; `Content-Disposition` names the date range (`all` when a bound is
absent).

## Notes

- These reads add **no** env var and **no** schema change — they synthesize `seq` / `event_type` /
  `broker_order_id` / `metadata` / `occurred_at` from the existing `order_events` columns.
- For the operator procedures (when to engage, the stage-flip rule), see
  [`../operations/kill-switch.md`](../operations/kill-switch.md).

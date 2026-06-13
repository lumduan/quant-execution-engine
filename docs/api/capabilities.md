# API — `GET /capabilities`

The static per-`(broker, market)` capability matrix the router enforces (D7), plus each configured
broker's runtime health. Public-mode readable (api-key-gated).

| | |
|---|---|
| Method / path | `GET /capabilities` |
| Gateway-proxied | `GET /api/v2/engines/execution/capabilities` |
| Auth | `X-API-Key` (public-mode readable) |
| Source | `src/quant_execution_engine/api/routes.py::capabilities` |

## Request

```bash
curl http://quant-api-gateway:8000/api/v2/engines/execution/capabilities \
  -H "X-API-Key: <your-api-key>"
```

## Response `200 OK` (abbreviated)

```json
{
  "stage": "sim",
  "capabilities": [
    {
      "broker": "sim",
      "market": "SET",
      "order_types": ["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "ICEBERG", "MTL", "ATO", "ATC"],
      "tifs": ["DAY", "IOC", "FOK", "GTC"],
      "position_effects": [],
      "amend": "native",
      "adapter_installed": true
    },
    {
      "broker": "liberator",
      "market": "TFEX",
      "order_types": ["MARKET", "LIMIT", "STOP", "STOP_LIMIT", "ICEBERG"],
      "tifs": ["DAY", "IOC", "FOK", "GTC"],
      "position_effects": ["OPEN", "CLOSE"],
      "amend": "cancel_replace",
      "adapter_installed": true
    },
    {
      "broker": "settrade",
      "market": "SET",
      "order_types": ["MARKET", "LIMIT", "MTL", "ATO", "ATC", "ICEBERG"],
      "tifs": ["DAY", "GTC", "IOC", "FOK"],
      "position_effects": [],
      "amend": "native",
      "adapter_installed": true
    }
  ],
  "brokers": null
}
```

| Field | Type | Meaning |
|-------|------|---------|
| `stage` | string | the active safety-ladder stage |
| `capabilities[]` | array | one `CapabilitySet` row per `(broker, market)` |
| `…broker` / `…market` | string | the cell's broker (`sim`/`liberator`/`settrade`) and market (`SET`/`TFEX`) |
| `…order_types` | array | supported `order_type` values for this cell |
| `…tifs` | array | supported `tif` values |
| `…position_effects` | array | `[]` for SET; `["OPEN","CLOSE"]` for TFEX |
| `…amend` | string | `"native"` (atomic, same cid) or `"cancel_replace"` (new cid) — read this before amending |
| `…adapter_installed` | bool | whether the adapter is wired (all six rows are `true` since Phase 4) |
| `brokers` | object \| null | same runtime-health block as `/health` (breaker + session), `null` when broker-free |

## Notes

- The full matrix has **six rows** — `{sim, liberator, settrade} × {SET, TFEX}`. The narrative
  reference (auth, cancel, query, stream, idempotency) is in
  [`../architecture/adapters.md`](../architecture/adapters.md).
- Submitting an unsupported `(broker, market, order_type, tif, position_effect)` is rejected
  pre-route with `capability_unsupported` (422). Notable gaps: **SET stops are unsupported on both
  real brokers**; Settrade TFEX has no `ATC`.
- The `amend` field is the contract for [`orders-amend.md`](orders-amend.md): Settrade is `native`,
  Liberator is `cancel_replace`. Never assume — read it here.

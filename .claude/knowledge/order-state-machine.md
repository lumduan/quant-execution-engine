# Order state machine

> Persisted in `quant-infra-db` (`execution.orders.status` + append-only
> `execution.order_events`). An app-level guard (and a DB constraint where practical) rejects
> illegal transitions. Every transition appends an immutable audit row. Realised in Phase 2 over
> the Phase-1 schema.

## States

| State | Kind | Meaning |
|---|---|---|
| `PENDING_NEW` | local | Persisted + deduped; not yet acked by the venue (the reconciliation window) |
| `NEW` | venue | Acked, resting at the venue; `broker_order_id` recorded |
| `PARTIALLY_FILLED` | venue | One or more partial fills; `remaining_qty > 0` |
| `FILLED` | terminal | Fully executed |
| `PENDING_CANCEL` | local | Cancel sent, awaiting venue confirm |
| `PENDING_REPLACE` | local | Amend (or Liberator cancel+replace) in flight |
| `CANCELLED` | terminal | Cancelled at the venue |
| `REJECTED` | terminal | Rejected pre-route (validation/risk) or by the venue |
| `EXPIRED` | terminal | Validity (e.g. DAY) lapsed |

## Legal transitions

```text
PENDING_NEW ──ack──────────► NEW
PENDING_NEW ──reject───────► REJECTED          (pre-route or venue reject)
NEW ──fill(partial)────────► PARTIALLY_FILLED
NEW ──fill(full)───────────► FILLED
NEW ──expire───────────────► EXPIRED
NEW/PARTIALLY_FILLED ──cancel-req──► PENDING_CANCEL ──confirm──► CANCELLED
NEW/PARTIALLY_FILLED ──amend-req───► PENDING_REPLACE ──confirm──► NEW   (qty/price updated)
PARTIALLY_FILLED ──fill(rest)──────► FILLED
PARTIALLY_FILLED ──expire──────────► EXPIRED
```

Terminal states accept no further transition. `FILLED`/`CANCELLED`/`REJECTED`/`EXPIRED` are
final.

## Idempotency & reconciliation hooks

- **Dedupe** happens at `submit` keyed on `client_order_id` **before** `PENDING_NEW` is even
  created twice — a repeat returns the existing order's result.
- The **`PENDING_NEW → NEW`** edge is where the `client_order_id ↔ broker_order_id` mapping is
  persisted atomically. If the ack is lost (network death after submit), the order is stuck in
  `PENDING_NEW`; the **reconciliation loop** queries broker truth (Liberator `GET /orders`,
  Settrade `get_orders`) and matches on `(account, symbol, side, qty, ts-window)` to recover
  the `broker_order_id` and advance the state — never blindly re-sending.
- Settrade fills arrive via the native `subscribe_derivatives_order` push; Liberator fills are
  reconciled from order queries. Both normalize to the same `PARTIALLY_FILLED`/`FILLED` edges.

## Amend asymmetry

- Settrade: `PENDING_REPLACE` resolves via native `change_order`.
- Liberator: no amend route — `PENDING_REPLACE` is implemented as cancel-then-replace
  (CANCELLED of the old `client_order_id` + a new `PENDING_NEW`), declared **non-atomic** so a
  caller knows the order can briefly rest at neither price.

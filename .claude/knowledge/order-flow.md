# Order flow — end-to-end

The verified path a normalized order takes, from a strategy to a durable, streamed result. Source of
truth: `core/router.py::OrderRouter`. The operator/consumer view is
[`docs/api/orders-submit.md`](../../docs/api/orders-submit.md); this is the agent reference for the
exact pipeline order.

## Submit path

```
strategy
  │  POST /api/v2/engines/execution/orders   (X-API-Key, X-Strategy-Id, NormalizedOrder)
  ▼
quant-api-gateway  ── proxy (forwards X-API-Key + X-Strategy-Id; gateway PR #24 stopped stripping it)
  │  POST :8400/orders
  ▼
OrderRouter.submit(order, strategy_id):
  1. kill_switch.assert_disengaged()        # FIRST (hard rule 3) — precedes even dedupe
  2. fetch_order_result(client_order_id)     # idempotency dedupe → prior result (200) if it exists
  3. capabilities.lookup(broker, market)     # capability gate
        .assert_supports(order_type, tif, position_effect)   # → capability_unsupported (422)
  4. RiskGate.check(order)                    # PTRM: per-order value/qty caps, per-account caps,
                                              #   rate limit (429), duplicate-burst guard (409)
  5. PriceBandCheck.check(order)              # advisory; no-op unless enabled + market-data configured
  6. resolve_adapter(stage, broker)          # stage ladder: sim/paper→Sim, micro_live→real, live→reject
  7. adapter.breaker.guard()                 # circuit breaker → broker_circuit_open (503) if open
  8. single_flight(redis, exe:submit:{cid}): # idempotent single-flight lock
        insert_order(order, strategy_id)      #   → PENDING_NEW (DB trigger writes the birth audit row)
        _place_and_settle(adapter, order)
  ▼
NormalizedOrderResult  (201 new / 200 duplicate)   — built from the durable row, raw excluded
```

`_place_and_settle`:

```
ack = adapter.place(order)
  rejected            → update_status(REJECTED) + set_reject_reason        → resolution=confirmed
  broker_order_id is None
                      → _recover_handle(order)                             → resolution=confirmed|pending|unknown
                        # TK-0423/TK-0424. NOT an error path: the Liberator ack carries no orderNo
                        # AT ALL, so this is that venue's NORMAL branch. Bursts against venue truth
                        # (250 ms cadence, 1500 ms budget anchored on the persisted submit ts) and
                        # resolves through the SAME reconciler matcher + executor as the steady loop.
                        # It used to `raise AdapterError` here → HTTP 500 for a LIVE order.
  else                → ack_order(cid, broker_order_id)        # PENDING_NEW→NEW, id stamped ATOMICALLY (§B)
                        for fill in ack.fills: apply_fill(...)  # NEW→PARTIALLY_FILLED / →FILLED; fills deduped
                        if ack.remainder_cancelled (IOC):       # PENDING_CANCEL → cancel() → CANCELLED
                                                                → resolution=confirmed
```

`resolution` rides on `SubmitOutcome` (transient per-submit knowledge, **not** persisted order state)
and is merged onto the `POST /orders` body only — `GET /orders/{cid}` deliberately omits it, because a
later read is not evidence about what was known at submit time. 🔴 `pending` (venue read, order
working) and `unknown` (venue **not** read, order may be live) never share a code path: only `unknown`
means the handle was not recovered, and a resubmit on it double-fills. Wire facts:
[`docs/reference/liberator-order-wire.md`](../../../docs/reference/liberator-order-wire.md) (umbrella).

## Cancel path (not kill-switch-gated)

`NEW`/`PARTIALLY_FILLED → PENDING_CANCEL → CANCELLED`. Terminal or transient (`PENDING_NEW`/
`PENDING_REPLACE`) source → `illegal_transition` (409). Re-cancel of `PENDING_CANCEL` is idempotent.
The kill-switch mass-cancel reuses this path on every open order.

## Amend path (kill-switch-gated FIRST)

Branches on `capabilities.lookup(broker, market).amend`:

- **native** (Settrade): `NEW → PENDING_REPLACE`, atomic `replace_order`, `→ NEW`. Venue reject =
  non-terminal restore (`PENDING_REPLACE → NEW`, `reject_reason` NOT written) + typed `amend_rejected`
  (409). PTRM re-checked, no exemption.
- **cancel_replace** (Liberator): cancel the old order (`PENDING_CANCEL` path) + `submit()` a fresh
  replacement under the required `new_client_order_id` (full pipeline again). No PTRM exemption.

## Fill cycle & reconciliation

Fills insert to `execution.fills` (deduped on `(client_order_id, broker_fill_id)`) and drive
`NEW→PARTIALLY_FILLED→FILLED`. A per-adapter reconciliation loop (~12 s) repairs drift against venue
truth: stuck `PENDING_NEW` → §B fuzzy match `(account, symbol, side, qty)` ±5 s → `NEW`, else
`REJECTED` after ~60 s (`ack_lost_unmatched`); stranded `PENDING_REPLACE` → Settrade `replace_resolve`.
It never blindly re-sends.

## Stream publish chain

Every transition funnels through the **five repository writers** —
`insert_order` / `ack_order` / `replace_order` / `update_status` / `apply_fill` — each of which calls
the in-process `EventHub.publish` **post-success, non-blocking, exception-proof**. The hub fans out to
SSE subscribers of `GET /orders/stream` (ring-buffer replay, `strategy_id`/`client_order_id` filters).
**The stream is advisory; the durable `execution.orders` store is truth** — a slow/dead subscriber
never blocks an order write.

See also: [`order-state-machine.md`](order-state-machine.md),
[`normalized-order-contract.md`](normalized-order-contract.md),
[`order-update-stream.md`](order-update-stream.md), [`capability-matrix.md`](capability-matrix.md).

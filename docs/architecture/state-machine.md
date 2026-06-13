# Architecture — Order State Machine

Every order moves through a **frozen 9-state machine with exactly 13 legal edges** (Phase 0 ADR §E).
The set is enforced **twice**: the pure app-side guard (`core/state_machine.py`) raises a clean typed
`illegal_transition` (409) before a DB round-trip, and the Phase-1 DB trigger
(`execution.orders_guard`) is the backstop — an illegal transition is a `check_violation` at the
database, never a silent write. The full transition table with triggers and notes is in
[`../data/state-machine-transitions.md`](../data/state-machine-transitions.md); this page explains
the shape.

## States

| State | Kind | Meaning |
|-------|------|---------|
| `PENDING_NEW` | entry / in-flight | Order persisted and submitted to the adapter; awaiting the broker ack |
| `NEW` | resting | Broker acked; `broker_order_id` stamped; resting at the venue |
| `PARTIALLY_FILLED` | resting | Some quantity filled; the remainder is still resting |
| `PENDING_CANCEL` | in-flight | A cancel was submitted; awaiting the venue's cancel ack |
| `PENDING_REPLACE` | in-flight | A **native** amend (Settrade) was submitted; awaiting the venue's amend ack |
| `FILLED` | **terminal** | Fully filled |
| `CANCELLED` | **terminal** | Cancelled (incl. the kill-switch mass-cancel sweep) |
| `REJECTED` | **terminal** | Broker rejected the order pre-route (from `PENDING_NEW`) |
| `EXPIRED` | **terminal** | TIF expiry |

The four **terminal** states are immutable — no edge leaves them. `PENDING_*` are **in-flight**
(transient) states that resolve via an ack or the reconciliation loop. Public callers see a frozen
6-value `PublicOrderStatus` (`NEW | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | EXPIRED`); the
truthful internal value always travels in the additive `engine_state` field (`PENDING_NEW` surfaces
as `NEW`, `PENDING_CANCEL`/`PENDING_REPLACE` as the closest fill-aware resting status).

## The 13 edges

```
  [submit]
     │
     ▼
 PENDING_NEW ──(1) broker ack (broker_order_id stamped atomically)──▶ NEW
 PENDING_NEW ──(2) broker pre-route reject────────────────────────▶ REJECTED ✗

 NEW ──(3) partial fill──────────────▶ PARTIALLY_FILLED
 NEW ──(4) full fill─────────────────▶ FILLED ✗
 NEW ──(5) TIF expiry────────────────▶ EXPIRED ✗
 NEW ──(6) cancel submitted──────────▶ PENDING_CANCEL
 NEW ──(7) native amend submitted────▶ PENDING_REPLACE

 PARTIALLY_FILLED ──(8) remainder fills────▶ FILLED ✗
 PARTIALLY_FILLED ──(9) TIF expiry─────────▶ EXPIRED ✗
 PARTIALLY_FILLED ──(10) cancel submitted──▶ PENDING_CANCEL
 PARTIALLY_FILLED ──(11) native amend──────▶ PENDING_REPLACE

 PENDING_CANCEL  ──(12) cancel ack──────────────────────────▶ CANCELLED ✗
 PENDING_REPLACE ──(13) amend ack OR amend-reject restore────▶ NEW

 ✗ = terminal (FILLED, CANCELLED, REJECTED, EXPIRED) — no edge leaves a terminal state.
 A same-state write (e.g. NEW→NEW) is a legal no-op.
```

Two things the diagram makes explicit (and that surprise newcomers):

- **There is no direct `*→CANCELLED` edge.** A cancel is always two steps —
  `NEW`/`PARTIALLY_FILLED → PENDING_CANCEL → CANCELLED`. The **kill-switch mass-cancel** is not a
  special edge; it reuses this ordinary cancel path on every open order.
- **`PENDING_REPLACE` is reserved for native amends (Settrade).** Liberator has no amend route, so
  its **cancel+replace** amend cancels the old order down the normal `PENDING_CANCEL` path and submits
  a fresh `PENDING_NEW` replacement — it never enters `PENDING_REPLACE`. Edge (13) is bidirectional in
  effect: a successful amend and a non-terminal amend-**reject** both land back on `NEW` (the order
  stays live; `reject_reason` is deliberately not written).

## Append-only audit

Every transition appends exactly one immutable row to `execution.order_events` (a DB trigger writes
it; another trigger blocks `UPDATE`/`DELETE`/`TRUNCATE`). The `broker_order_id` and the amended
`price`/`quantity` are snapshotted **atomically** with the transition (the `PENDING_NEW → NEW` row
captures the broker id; the `PENDING_REPLACE → NEW` row captures the amended values). The audit trail
is read via [`../api/admin.md`](../api/admin.md) (`GET /admin/orders/{cid}/audit`,
`GET /admin/audit/export`) — synthesized from these rows, no extra storage. See
[`../data/execution-schema.md`](../data/execution-schema.md).

## Idempotency

Every order carries a client-generated `client_order_id` (UUIDv4). The engine dedupes **before
routing**: a re-submitted id returns the prior ack with no second broker call (the router's first
post-kill-switch step is a `client_order_id` lookup). The `client_order_id` **primary key** on
`execution.orders` is the idempotency constraint — a race that slips past the in-app check is caught
by the PK and resolved to the prior result. This is the "at-least-once + dedupe", not exactly-once,
guarantee (ADR §A).

## The reconciliation window

`PENDING_*` states are never permanent. A background reconciliation loop (per adapter, ~12 s default)
repairs submit/ack drift against broker truth:

- **`PENDING_NEW` stuck (lost ack, §B):** fuzzy-match the venue's open orders on
  `(account, symbol, side, quantity)` within **±5 s** of the persisted submit time; on a unique match
  the order is acked to `NEW`, otherwise it resolves to `REJECTED` after a bounded
  `ack_lost_unmatched` window (~60 s). Reconciliation never blindly re-sends.
- **`PENDING_REPLACE` stranded (Settrade):** the `replace_resolve` action restores the order to `NEW`
  with the venue's current resting values.
- **`PENDING_CANCEL` stuck:** re-confirmed against the venue and driven to `CANCELLED`.

See [`adapters.md`](adapters.md) for the per-adapter reconcilers and
[`../operations/troubleshooting.md`](../operations/troubleshooting.md) for the operator view.

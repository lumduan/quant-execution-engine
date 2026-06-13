# Data — State-Machine Transitions

The **13 frozen legal edges**, exactly as encoded in the `execution.orders_guard` DB trigger
(`12_schema_execution.sql`) and the app-side guard (`core/state_machine.py`). Any transition not in
this table is rejected — at the app boundary as a typed `illegal_transition` (409), and at the
database as a `check_violation`. For the narrative see
[`../architecture/state-machine.md`](../architecture/state-machine.md).

| # | From | To | Trigger (event) | DB-enforced | Notes |
|:-:|------|----|-----------------|:-----------:|-------|
| 1 | `PENDING_NEW` | `NEW` | broker ack (place response carries `broker_order_id`) | ✅ | `broker_order_id` stamped atomically (§B); the audit row snapshots it |
| 2 | `PENDING_NEW` | `REJECTED` | broker pre-route reject | ✅ | `reject_reason` persisted; terminal |
| 3 | `NEW` | `PARTIALLY_FILLED` | fill event (partial) | ✅ | a row is inserted to `execution.fills` |
| 4 | `NEW` | `FILLED` | fill event (full) | ✅ | terminal |
| 5 | `NEW` | `EXPIRED` | TIF expiry | ✅ | terminal |
| 6 | `NEW` | `PENDING_CANCEL` | cancel submitted | ✅ | |
| 7 | `NEW` | `PENDING_REPLACE` | native amend submitted (Settrade) | ✅ | Liberator amend is cancel+replace — it does **not** use this edge |
| 8 | `PARTIALLY_FILLED` | `FILLED` | fill event (remainder) | ✅ | terminal |
| 9 | `PARTIALLY_FILLED` | `EXPIRED` | TIF expiry | ✅ | terminal |
| 10 | `PARTIALLY_FILLED` | `PENDING_CANCEL` | cancel submitted | ✅ | |
| 11 | `PARTIALLY_FILLED` | `PENDING_REPLACE` | native amend submitted (Settrade) | ✅ | |
| 12 | `PENDING_CANCEL` | `CANCELLED` | cancel ack | ✅ | the kill-switch mass-cancel sweep resolves here too |
| 13 | `PENDING_REPLACE` | `NEW` | amend ack **or** amend-reject restore | ✅ | non-terminal: a venue amend-reject restores the live order here (`reject_reason` not written) |

**Terminal states** (immutable — no edge leaves them): `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`.

**A same-state write** (e.g. `NEW → NEW`, or a second `PARTIALLY_FILLED` for an additional partial
fill) is a legal no-op and appends **no** audit row — fills are audited in `execution.fills`.

## What is deliberately NOT an edge

These are excluded by design; adding any would require an ADR amendment **then** a schema migration:

- **`* → CANCELLED` directly.** Cancel is always two-step (`…→PENDING_CANCEL→CANCELLED`, edges 6/10 →
  12). The kill-switch mass-cancel reuses this path; it is not a special edge.
- **`PENDING_REPLACE → CANCELLED`.** Liberator's cancel+replace amend cancels the old order down the
  ordinary `PENDING_CANCEL` path and submits a fresh `PENDING_NEW` replacement — it never strands an
  order in `PENDING_REPLACE`. That state is Settrade-native-amend-only.
- **Fills while `PENDING_CANCEL`**, and any venue rejecting a cancel — both resolve through the
  reconciliation loop, not via a new edge.

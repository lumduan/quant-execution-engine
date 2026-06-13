# Data — Execution Schema (`db_execution`)

The durable order store lives in its **own** Postgres database `db_execution`, schema `execution`,
owned by `quant-infra-db` (init scripts `12_schema_execution.sql` + `13_execution_strategy_id.sql`).
The standalone `quant-execution-engine` is the sole writer; strategies and the gateway never write
`execution.*`. For the lifecycle these tables record, see
[`../architecture/state-machine.md`](../architecture/state-machine.md) and
[`state-machine-transitions.md`](state-machine-transitions.md).

## Why plain Postgres (not TimescaleDB)

This is the **low-volume real-money command plane** — tens of rows a day, not ticks. The tables use
**plain Postgres**, not hypertables: `fills` and `order_events` carry foreign keys to `orders`, and a
hypertable cannot be an FK target; chunking/compression/retention buy nothing here and would endanger
audit rows. Enums are `TEXT` + `CHECK` (idempotent under re-run, evolvable, and violations raise plain
`check_violation`).

## `execution.orders`

One row per client order. The **primary key on `client_order_id` is the idempotency constraint** — a
re-submit collides and the engine returns the prior result.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `client_order_id` | TEXT | **PK**; `char_length BETWEEN 8 AND 64` | UUIDv4 (opaque — format validated at the engine boundary, never parsed in SQL) |
| `broker` | TEXT | `CHECK in (sim, liberator, settrade)` | |
| `broker_order_id` | TEXT | nullable | venue id; **NULL until ack** (§B), then stamped atomically with `PENDING_NEW→NEW` |
| `account` | TEXT | NOT NULL | never logged |
| `symbol` | TEXT | NOT NULL | |
| `market` | TEXT | `CHECK in (SET, TFEX)` | |
| `side` | TEXT | `CHECK in (BUY, SELL)` | |
| `order_type` | TEXT | `CHECK in (MARKET, LIMIT, STOP, STOP_LIMIT, ICEBERG, MTL, ATO, ATC)` | |
| `price` | NUMERIC(18,6) | `NULL OR > 0` | **never float**; required for LIMIT/STOP_LIMIT (CHECK) |
| `stop_price` | NUMERIC(18,6) | `NULL OR > 0` | required for STOP/STOP_LIMIT (CHECK) |
| `quantity` | BIGINT | `> 0` | integral (SET shares / TFEX contracts) |
| `display_qty` | BIGINT | `NULL OR (> 0 AND <= quantity)` | ICEBERG only |
| `tif` | TEXT | `CHECK in (DAY, IOC, FOK, GTC)` | |
| `position_effect` | TEXT | `NULL OR in (OPEN, CLOSE)`; `NULL OR market='TFEX'` | required for TFEX, omitted for SET |
| `status` | TEXT | DEFAULT `PENDING_NEW`; `CHECK in (`9 states`)` | the internal 9-state machine |
| `reject_reason` | TEXT | nullable | persisted on the `REJECTED` row (real-money audit, not a cache) |
| `created_at` / `updated_at` | TIMESTAMPTZ | DEFAULT `now()` | UTC; `updated_at` bumped by the guard trigger |
| `strategy_id` | TEXT | nullable (Phase 5) | the `X-Strategy-Id` (D16); not part of the frozen `NormalizedOrder` |

**Indexes:**

| Index | Columns | Purpose |
|-------|---------|---------|
| `pk_orders` | `(client_order_id)` | idempotency PK |
| `idx_orders_broker_order_id` | `(broker, broker_order_id) WHERE broker_order_id IS NOT NULL` | venue→local lookup (partial; not unique — venue ids recycle) |
| `idx_orders_reconcile` | `(account, symbol, side, quantity, created_at)` | the §B reconciliation fuzzy-match scan |
| `idx_orders_strategy` | `(strategy_id, created_at) WHERE strategy_id IS NOT NULL` | the stream-filter seed path (partial) |

## `execution.fills`

One row per fill event. **`UNIQUE (client_order_id, broker_fill_id)` dedupes at-least-once venue fill
delivery** (a NULL `broker_fill_id` is *not* deduped — adapters must supply an id; `SimAdapter`
synthesizes one).

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `fill_id` | BIGINT | **PK**, `GENERATED ALWAYS AS IDENTITY` | insertion-ordered |
| `client_order_id` | TEXT | FK → `orders` | |
| `broker_fill_id` | TEXT | part of `uq_fills_order_broker_fill` | venue fill id (or synthesized) |
| `price` | NUMERIC(18,6) | `> 0` | **never float** |
| `quantity` | BIGINT | `> 0` | |
| `exec_ts` | TIMESTAMPTZ | NOT NULL | venue execution time (not the DB row time) |
| `created_at` | TIMESTAMPTZ | DEFAULT `now()` | |

## `execution.order_events`

The **append-only audit log** — one immutable row per transition, written by trigger (the `INSERT`
writes the `NULL → PENDING_NEW` birth row).

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `event_id` | BIGINT | **PK**, `GENERATED ALWAYS AS IDENTITY` | monotonic (total order even within one txn) |
| `client_order_id` | TEXT | FK → `orders` | every order has ≥ 1 event ⇒ orders can never be deleted |
| `from_status` | TEXT | nullable; 9-state CHECK | NULL on the birth row |
| `to_status` | TEXT | NOT NULL; 9-state CHECK | |
| `event` | JSONB | | snapshots `broker_order_id` / `price` / `quantity` at the transition |
| `created_at` | TIMESTAMPTZ | DEFAULT `now()` | |

Index `idx_order_events_order` on `(client_order_id, event_id)` is the audit-read path. The
`GET /admin/orders/{cid}/audit` + `GET /admin/audit/export` endpoints synthesize `seq` / `event_type` /
`broker_order_id` / `metadata` / `occurred_at` from these columns — no extra storage.

## Triggers (the DB-enforced invariants)

| Trigger | Fires | Enforces |
|---------|-------|----------|
| `trg_orders_guard` | BEFORE INSERT/UPDATE on `orders` | Entry must be `PENDING_NEW`; a status change must be one of the **13 legal edges** (else `check_violation`); bumps `updated_at` |
| `trg_orders_append_event` | AFTER INSERT/UPDATE on `orders` | Appends one audit row per INSERT / status change, snapshotting `broker_order_id`/`price`/`quantity` |
| `trg_order_events_no_update_delete` / `_no_truncate` | BEFORE UPDATE/DELETE/TRUNCATE on `order_events` | Append-only — any mutation raises |

The guard's `legal` array is exactly the 13 edges in [`state-machine-transitions.md`](state-machine-transitions.md);
terminal immutability falls out (no edge sources a terminal state). Same-status UPDATEs (e.g. a second
partial fill) are allowed and append no event — fills are audited in `execution.fills`.

## Grants (least-privilege)

The shared `quant` service role (created with a placeholder password operators **must** override):

- `orders`: `SELECT, INSERT, UPDATE` (the lifecycle writer)
- `fills`: `SELECT, INSERT`
- `order_events`: `SELECT, INSERT` (INSERT is required because the audit trigger runs with invoker
  rights; append-only still holds — no UPDATE/DELETE granted, and the block-mutation triggers reject
  them for every role)
- sequences: `USAGE, SELECT`
- **No `DELETE` anywhere; strategies and the gateway get no grant at all.**

"""Repository functions over the Phase-1 ``execution`` store.

Hard constraints (Phase 1 design — DB triggers own the audit trail):

* NEVER insert/update ``execution.order_events`` — the ``orders_append_event``
  trigger auto-appends exactly one audit row per INSERT/transition.
* The ack is ONE UPDATE setting ``status='NEW'`` + ``broker_order_id`` so the
  trigger snapshots the §B id-mapping atomically with the transition.
* SQLSTATE 23514 means TWO different things and is mapped separately in each:
  UPDATE -> ``orders_guard`` trigger -> :class:`IllegalTransition`;
  INSERT -> a column CHECK -> :class:`StoreConstraintViolated` (TK-0395);
  the app-side :mod:`core.state_machine` guard runs first for clean errors and
  the DB stays the backstop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

import asyncpg

from src.quant_execution_engine.contracts.enums import Broker, OrderState
from src.quant_execution_engine.contracts.errors import IllegalTransition, StoreConstraintViolated
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core import state_machine
from src.quant_execution_engine.db.errors import DuplicateOrderSignal, RepositoryError
from src.quant_execution_engine.db.models import OrderEventRow, OrderResultRow, OrderRow
from src.quant_execution_engine.events.hub import get_event_hub
from src.quant_execution_engine.events.models import FillEvent

logger = logging.getLogger(__name__)

_INSERT_ORDER = (
    "INSERT INTO execution.orders "
    "(client_order_id, broker, broker_order_id, account, symbol, market, side, "
    "order_type, price, stop_price, quantity, display_qty, tif, position_effect, "
    "strategy_id) "
    "VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)"
)

_SELECT_ORDER = "SELECT * FROM execution.orders WHERE client_order_id = $1"

_SELECT_ORDER_RESULT = (
    "SELECT o.*, COALESCE(SUM(f.quantity), 0)::bigint AS filled_qty, "
    "SUM(f.price * f.quantity) AS fill_notional "
    "FROM execution.orders o "
    "LEFT JOIN execution.fills f USING (client_order_id) "
    "WHERE o.client_order_id = $1 "
    "GROUP BY o.client_order_id"
)

_ACK_ORDER = (
    "UPDATE execution.orders SET status = 'NEW', broker_order_id = $2 "
    "WHERE client_order_id = $1 AND status = 'PENDING_NEW'"
)

_REPLACE_ORDER = (
    "UPDATE execution.orders "
    "SET status = 'NEW', price = COALESCE($2, price), quantity = COALESCE($3, quantity) "
    "WHERE client_order_id = $1 AND status = 'PENDING_REPLACE'"
)

_UPDATE_STATUS = "UPDATE execution.orders SET status = $2 WHERE client_order_id = $1"

_SET_REJECT_REASON = "UPDATE execution.orders SET reject_reason = $2 WHERE client_order_id = $1"

_INSERT_FILL = (
    "INSERT INTO execution.fills (client_order_id, broker_fill_id, price, quantity, exec_ts) "
    "VALUES ($1, $2, $3, $4, $5) "
    "ON CONFLICT (client_order_id, broker_fill_id) DO NOTHING"
)

_SUM_FILLS = (
    "SELECT COALESCE(SUM(quantity), 0)::bigint FROM execution.fills WHERE client_order_id = $1"
)

_SELECT_OPEN_ORDERS = (
    "SELECT * FROM execution.orders WHERE status IN ('NEW', 'PARTIALLY_FILLED') ORDER BY created_at"
)

_RECONCILE_STATES: tuple[str, ...] = ("PENDING_NEW", "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL")

_SELECT_RECONCILE_ORDERS = (
    "SELECT * FROM execution.orders WHERE broker = $1 AND status = ANY($2::text[]) "
    "ORDER BY created_at"
)

_SELECT_CIDS_FOR_STRATEGY = "SELECT client_order_id FROM execution.orders WHERE strategy_id = $1"

# Audit read (Phase 6 / E1): all events for one order, in total order. event_id
# is monotonic even when created_at ties inside a single transaction.
_SELECT_ORDER_EVENTS = (
    "SELECT event_id, from_status, to_status, event, created_at "
    "FROM execution.order_events WHERE client_order_id = $1 ORDER BY event_id"
)

# Audit export (Phase 6 / E2): every order_events row joined to its order for the
# strategy attribution, streamed via a server-side cursor (never buffered). The
# optional bounds/filter are appended positionally so the cursor sees only $-args.
_EXPORT_ORDER_EVENTS = (
    "SELECT e.event_id, e.client_order_id, e.from_status, e.to_status, "
    "e.event, e.created_at, o.strategy_id "
    "FROM execution.order_events e "
    "JOIN execution.orders o USING (client_order_id)"
)

# How many rows the server-side cursor materialises per round trip (E2). Bounded
# so a large date-range export never buffers the whole result set in memory.
_EXPORT_CURSOR_BATCH = 500


def _rowcount(command_tag: str) -> int:
    """Parse the trailing row count from an asyncpg command tag (e.g. ``UPDATE 1``)."""
    try:
        return int(command_tag.rsplit(" ", 1)[-1])
    except ValueError as exc:  # pragma: no cover - defensive
        raise RepositoryError(f"unparseable command tag: {command_tag!r}") from exc


async def insert_order(
    pool: asyncpg.Pool, order: NormalizedOrder, strategy_id: str | None = None
) -> None:
    """Persist the order at the entry state (DB DEFAULT ``PENDING_NEW``).

    Committed BEFORE any venue I/O (hard rule 5). A PK collision raises
    :class:`DuplicateOrderSignal` — the durable dedupe backstop. ``strategy_id``
    is the persisted ``X-Strategy-Id`` (D16); on success the hub records the
    cid→strategy attribution and emits the ``PENDING_NEW`` birth event.
    """
    try:
        await pool.execute(
            _INSERT_ORDER,
            order.client_order_id,
            order.broker.value,
            order.account,
            order.symbol,
            order.market.value,
            order.side.value,
            order.order_type.value,
            order.price,
            order.stop_price,
            order.quantity,
            order.display_qty,
            order.tif.value,
            order.position_effect.value if order.position_effect else None,
            strategy_id,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise DuplicateOrderSignal(order.client_order_id) from exc
    except asyncpg.exceptions.CheckViolationError as exc:
        # 23514 on the INSERT — a column CHECK the row does not satisfy. Mapped rather than
        # allowed to escape: a bare 500 reads as RETRYABLE to every calling adapter, so a
        # permanent schema mismatch would wear a transient signature and be retried forever
        # (TK-0395; the instance that exposed it was the stale orders_broker_check, #183).
        raise StoreConstraintViolated(str(exc), client_order_id=order.client_order_id) from exc
    hub = get_event_hub()
    if hub is not None:
        if strategy_id is not None:
            hub.register_strategy(order.client_order_id, strategy_id)
        hub.publish(
            client_order_id=order.client_order_id,
            engine_state=OrderState.PENDING_NEW,
            strategy_id=strategy_id,
        )


async def fetch_order(pool: asyncpg.Pool, client_order_id: str) -> OrderRow | None:
    """Return the bare order row, or None."""
    record = await pool.fetchrow(_SELECT_ORDER, client_order_id)
    return OrderRow.from_record(record) if record is not None else None


async def fetch_order_result(pool: asyncpg.Pool, client_order_id: str) -> OrderResultRow | None:
    """Return the order joined with its fill aggregate, or None."""
    record = await pool.fetchrow(_SELECT_ORDER_RESULT, client_order_id)
    return OrderResultRow.from_record(record) if record is not None else None


async def ack_order(pool: asyncpg.Pool, client_order_id: str, broker_order_id: str) -> None:
    """PENDING_NEW -> NEW + record the venue id, atomically (§B).

    One statement so the audit trigger snapshots ``broker_order_id`` in the
    same transaction as the transition.
    """
    tag = await pool.execute(_ACK_ORDER, client_order_id, broker_order_id)
    if _rowcount(tag) == 0:
        raise IllegalTransition(
            "ack requires the order to be in PENDING_NEW",
            client_order_id=client_order_id,
        )
    hub = get_event_hub()
    if hub is not None:
        hub.publish(
            client_order_id=client_order_id,
            engine_state=OrderState.NEW,
            broker_order_id=broker_order_id,
        )


async def replace_order(
    pool: asyncpg.Pool,
    client_order_id: str,
    new_price: Decimal | None,
    new_qty: int | None,
) -> None:
    """PENDING_REPLACE -> NEW + the amended price/qty, atomically (native amend).

    One statement so the audit trigger snapshots the amended ``price``/``quantity``
    in the same transaction as the ``PENDING_REPLACE->NEW`` transition (the
    trigger reads ``NEW.price``/``NEW.quantity`` on every status change). A
    ``COALESCE`` keeps the unchanged column — ``price`` is never set NULL. The
    rowcount guard mirrors :func:`ack_order`: zero rows means the order was not
    in PENDING_REPLACE, which is an illegal transition.
    """
    tag = await pool.execute(_REPLACE_ORDER, client_order_id, new_price, new_qty)
    if _rowcount(tag) == 0:
        raise IllegalTransition(
            "replace requires the order to be in PENDING_REPLACE",
            client_order_id=client_order_id,
        )
    hub = get_event_hub()
    if hub is not None:
        # NEW (the amended-resting state) carrying the amended values so a
        # subscriber sees the new price/qty without a re-read.
        hub.publish(
            client_order_id=client_order_id,
            engine_state=OrderState.NEW,
            price=new_price,
            quantity=new_qty,
        )


async def update_status(pool: asyncpg.Pool, client_order_id: str, new_status: OrderState) -> None:
    """Advance the order state (app guard first; DB trigger is the backstop)."""
    row = await fetch_order(pool, client_order_id)
    if row is None:
        raise IllegalTransition(
            "cannot transition an unknown order", client_order_id=client_order_id
        )
    if row.status is new_status:
        return  # same-status is a legal no-op; skip the pointless write (and event)
    state_machine.assert_legal(row.status, new_status, client_order_id=client_order_id)
    try:
        await pool.execute(_UPDATE_STATUS, client_order_id, new_status.value)
    except asyncpg.exceptions.CheckViolationError as exc:
        raise IllegalTransition(str(exc), client_order_id=client_order_id) from exc
    hub = get_event_hub()
    if hub is not None:
        hub.publish(
            client_order_id=client_order_id,
            engine_state=new_status,
            strategy_id=row.strategy_id,
            broker_order_id=row.broker_order_id,
        )


async def set_reject_reason(pool: asyncpg.Pool, client_order_id: str, reason: str) -> None:
    """Persist the adapter/venue reject reason durably (Phase-2 column)."""
    await pool.execute(_SET_REJECT_REASON, client_order_id, reason)


async def apply_fill(
    pool: asyncpg.Pool,
    client_order_id: str,
    *,
    broker_fill_id: str,
    price: Decimal,
    quantity: int,
    exec_ts: datetime,
    total_quantity: int,
) -> OrderState:
    """Record one fill and flip the state if the aggregate changed it.

    One transaction: the fill row and the status flip commit atomically.
    At-least-once safe — a redelivered ``broker_fill_id`` no-ops via
    ``ON CONFLICT DO NOTHING``. Reused verbatim by the Phase-3/4 stream and
    reconcile workers.
    """
    async with pool.acquire() as conn, conn.transaction():
        insert_tag = await conn.execute(
            _INSERT_FILL, client_order_id, broker_fill_id, price, quantity, exec_ts
        )
        filled = await conn.fetchval(_SUM_FILLS, client_order_id)
        target = OrderState.FILLED if int(filled) >= total_quantity else OrderState.PARTIALLY_FILLED
        current = await conn.fetchval(
            "SELECT status FROM execution.orders WHERE client_order_id = $1",
            client_order_id,
        )
        if current != target.value:
            try:
                await conn.execute(_UPDATE_STATUS, client_order_id, target.value)
            except asyncpg.exceptions.CheckViolationError as exc:
                raise IllegalTransition(str(exc), client_order_id=client_order_id) from exc
    # Publish AFTER the transaction commits (never inside it). A redelivered fill
    # (ON CONFLICT DO NOTHING ⇒ "INSERT 0 0") is a no-op and emits nothing — the
    # stream stays at-least-once-clean like the durable write.
    hub = get_event_hub()
    if hub is not None and _rowcount(insert_tag) > 0:
        hub.publish(
            client_order_id=client_order_id,
            engine_state=target,
            fill=FillEvent(
                broker_fill_id=broker_fill_id,
                price=price,
                quantity=quantity,
                exec_ts=exec_ts,
            ),
        )
    return target


async def fetch_open_orders(pool: asyncpg.Pool) -> list[OrderRow]:
    """All venue-resting orders (NEW / PARTIALLY_FILLED) — the mass-cancel set."""
    records = await pool.fetch(_SELECT_OPEN_ORDERS)
    return [OrderRow.from_record(r) for r in records]


async def fetch_orders_for_reconcile(
    pool: asyncpg.Pool,
    broker: Broker,
    *,
    include_pending_replace: bool = False,
) -> list[OrderRow]:
    """Non-terminal rows for one broker — the reconciliation working set (§B).

    Includes ``PENDING_NEW`` (lost-ack candidates) and ``PENDING_CANCEL``
    (stuck-cancel candidates) on top of the venue-resting states. A native-amend
    broker passes ``include_pending_replace=True`` so a stranded
    ``PENDING_REPLACE`` (crash/lost-ack mid-amend) is repaired by its reconciler;
    the default keeps the cancel_replace working set (Liberator, Streaming Pro)
    unchanged.
    """
    states = _RECONCILE_STATES
    if include_pending_replace:
        states = (*states, "PENDING_REPLACE")
    records = await pool.fetch(_SELECT_RECONCILE_ORDERS, broker.value, list(states))
    return [OrderRow.from_record(r) for r in records]


async def fetch_client_order_ids_for_strategy(pool: asyncpg.Pool, strategy_id: str) -> set[str]:
    """All cids ever submitted under ``strategy_id`` (the stream seed set, D16).

    ``GET /orders/stream?strategy_id=`` loads this once at subscribe time so that
    reconciler-/restart-discovered events (which carry no in-memory attribution)
    still match the strategy filter. Low-volume table; the partial index on
    ``strategy_id`` covers it.
    """
    records = await pool.fetch(_SELECT_CIDS_FOR_STRATEGY, strategy_id)
    return {r["client_order_id"] for r in records}


async def fetch_order_events(pool: asyncpg.Pool, client_order_id: str) -> list[OrderEventRow]:
    """All audit rows for one order, in total (``event_id``) order (E1 read).

    Read-only — ``execution.order_events`` is trigger-written and append-only.
    The synthesized audit response (``api/audit.py``) derives ``seq``/``event_type``
    from these rows; the ``event`` JSONB rides through verbatim as ``metadata``.
    """
    records = await pool.fetch(_SELECT_ORDER_EVENTS, client_order_id)
    return [OrderEventRow.from_record(r) for r in records]


def _export_query(
    from_ts: datetime | None,
    to_ts: datetime | None,
    strategy_id: str | None,
) -> tuple[str, list[object]]:
    """Build the export SELECT + positional args for the optional filters (E2).

    ``from_ts`` is inclusive (``created_at >= $``), ``to_ts`` exclusive
    (``created_at < $``), ``strategy_id`` joins on ``orders.strategy_id``. Args are
    bound positionally so the server-side cursor only ever sees ``$n`` parameters
    (no string interpolation of values — injection-safe).
    """
    clauses: list[str] = []
    args: list[object] = []
    if from_ts is not None:
        args.append(from_ts)
        clauses.append(f"e.created_at >= ${len(args)}")
    if to_ts is not None:
        args.append(to_ts)
        clauses.append(f"e.created_at < ${len(args)}")
    if strategy_id is not None:
        args.append(strategy_id)
        clauses.append(f"o.strategy_id = ${len(args)}")
    sql = _EXPORT_ORDER_EVENTS
    if clauses:
        sql = f"{sql} WHERE {' AND '.join(clauses)}"
    sql = f"{sql} ORDER BY e.event_id"
    return sql, args


async def stream_order_events(
    pool: asyncpg.Pool,
    *,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    strategy_id: str | None = None,
) -> AsyncIterator[dict[str, object]]:
    """Yield export rows one at a time via an asyncpg server-side cursor (E2).

    A cursor inside a transaction streams in ``_EXPORT_CURSOR_BATCH`` round-trips
    so a large date range never buffers the whole result set in memory. Each yield
    is a plain JSON-ready dict (the NDJSON line shape): the ``event`` JSONB is
    decoded once here so the route serialises a real object, not a quoted string.
    """
    sql, args = _export_query(from_ts, to_ts, strategy_id)
    async with pool.acquire() as conn, conn.transaction():
        # ``prefetch`` bounds the per-round-trip batch (the "FETCH 500" intent):
        # asyncpg streams the result set in chunks, never the whole thing at once.
        cursor = conn.cursor(sql, *args, prefetch=_EXPORT_CURSOR_BATCH)
        async for record in cursor:
            raw_event = record["event"]
            event = json.loads(raw_event) if isinstance(raw_event, str) else raw_event
            ts = record["created_at"]
            yield {
                "event_id": record["event_id"],
                "client_order_id": record["client_order_id"],
                "from_status": record["from_status"],
                "to_status": record["to_status"],
                "event": event,
                "strategy_id": record["strategy_id"],
                "created_at": ts.isoformat() if ts is not None else None,
            }

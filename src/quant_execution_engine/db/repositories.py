"""Repository functions over the Phase-1 ``execution`` store.

Hard constraints (Phase 1 design — DB triggers own the audit trail):

* NEVER insert/update ``execution.order_events`` — the ``orders_append_event``
  trigger auto-appends exactly one audit row per INSERT/transition.
* The ack is ONE UPDATE setting ``status='NEW'`` + ``broker_order_id`` so the
  trigger snapshots the §B id-mapping atomically with the transition.
* Illegal transitions raise SQLSTATE 23514 from the ``orders_guard`` trigger;
  the app-side :mod:`core.state_machine` guard runs first for clean errors and
  the DB stays the backstop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

import asyncpg

from src.quant_execution_engine.contracts.enums import Broker, OrderState
from src.quant_execution_engine.contracts.errors import IllegalTransition
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core import state_machine
from src.quant_execution_engine.db.errors import DuplicateOrderSignal, RepositoryError
from src.quant_execution_engine.db.models import OrderResultRow, OrderRow

logger = logging.getLogger(__name__)

_INSERT_ORDER = (
    "INSERT INTO execution.orders "
    "(client_order_id, broker, broker_order_id, account, symbol, market, side, "
    "order_type, price, stop_price, quantity, display_qty, tif, position_effect) "
    "VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)"
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


def _rowcount(command_tag: str) -> int:
    """Parse the trailing row count from an asyncpg command tag (e.g. ``UPDATE 1``)."""
    try:
        return int(command_tag.rsplit(" ", 1)[-1])
    except ValueError as exc:  # pragma: no cover - defensive
        raise RepositoryError(f"unparseable command tag: {command_tag!r}") from exc


async def insert_order(pool: asyncpg.Pool, order: NormalizedOrder) -> None:
    """Persist the order at the entry state (DB DEFAULT ``PENDING_NEW``).

    Committed BEFORE any venue I/O (hard rule 5). A PK collision raises
    :class:`DuplicateOrderSignal` — the durable dedupe backstop.
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
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        raise DuplicateOrderSignal(order.client_order_id) from exc


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


async def update_status(pool: asyncpg.Pool, client_order_id: str, new_status: OrderState) -> None:
    """Advance the order state (app guard first; DB trigger is the backstop)."""
    row = await fetch_order(pool, client_order_id)
    if row is None:
        raise IllegalTransition(
            "cannot transition an unknown order", client_order_id=client_order_id
        )
    if row.status is new_status:
        return  # same-status is a legal no-op; skip the pointless write
    state_machine.assert_legal(row.status, new_status, client_order_id=client_order_id)
    try:
        await pool.execute(_UPDATE_STATUS, client_order_id, new_status.value)
    except asyncpg.exceptions.CheckViolationError as exc:
        raise IllegalTransition(str(exc), client_order_id=client_order_id) from exc


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
        await conn.execute(_INSERT_FILL, client_order_id, broker_fill_id, price, quantity, exec_ts)
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
    (stuck-cancel candidates) on top of the venue-resting states. Native-amend
    brokers (Settrade) pass ``include_pending_replace=True`` so a stranded
    ``PENDING_REPLACE`` (crash/lost-ack mid-amend) is repaired by the reconciler;
    the default keeps the Liberator (cancel_replace) working set unchanged.
    """
    states = _RECONCILE_STATES
    if include_pending_replace:
        states = (*states, "PENDING_REPLACE")
    records = await pool.fetch(_SELECT_RECONCILE_ORDERS, broker.value, list(states))
    return [OrderRow.from_record(r) for r in records]

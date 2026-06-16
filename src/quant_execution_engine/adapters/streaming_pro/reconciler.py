"""Reconciliation loop v1 — repairs submit/ack drift against venue truth (§B).

Mirrors the Liberator reconciler: every pass polls ``GET /orders`` per ``(account, market)`` with
non-terminal streaming_pro rows and drives the frozen 13-edge machine through the Phase-2 repository
seams (``ack_order`` / ``apply_fill`` / ``update_status``):

* lost ack: a ``PENDING_NEW`` stuck >5 s fuzzy-matches ``(symbol, side, qty)`` with venue
  ``entryTime`` within ±5 s of the persisted submit ts; a unique match acks, an ambiguous one is
  skipped, an unmatched row past 60 s resolves bounded to ``REJECTED`` — the loop NEVER re-sends;
* fills: venue ``matchQty`` is cumulative — the delta vs the durable fill aggregate becomes one fill
  with the deterministic id ``{order_no}:{matched}`` (re-polls dedupe via ``ON CONFLICT``);
* venue terminals map onto legal edges only (cancelled → two-step, expired → EXPIRED, a post-ack
  reject → reason + cancel path).

``plan_actions`` is pure (the parametrized-test surface). Account numbers never logged. The exact
``/orders`` list-row field mapping is verified in a ``micro_live`` soak (documented).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg

from src.quant_execution_engine.adapters.streaming_pro.adapter import StreamingProAdapter
from src.quant_execution_engine.adapters.streaming_pro.errors import StreamingProTransportError
from src.quant_execution_engine.adapters.streaming_pro.mapping import (
    VenueOrderState,
    classify_venue_state,
    from_venue_side,
)
from src.quant_execution_engine.adapters.streaming_pro.models import VenueOrderRow
from src.quant_execution_engine.contracts.enums import Broker, Market, OrderState
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.models import OrderRow

logger = logging.getLogger(__name__)

_STUCK_PENDING_SECONDS = 5.0
_ACK_LOST_TIMEOUT_SECONDS = 60.0
_FUZZY_WINDOW_SECONDS = 5.0


@dataclass(frozen=True)
class ReconcileAction:
    """One persistence step the executor applies (smallest testable unit)."""

    kind: str  # ack | fill | cancel_two_step | cancel_confirm | expire | reject | post_ack_reject
    client_order_id: str
    broker_order_id: str | None = None
    fill_qty: int = 0
    fill_price: Decimal | None = None
    fill_id: str | None = None
    total_quantity: int = 0
    reason: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def fill_price_for(row: OrderRow, item: VenueOrderRow) -> Decimal | None:
    """v1 fill-price: venue order price, else the local limit (no per-fill stream until P5)."""
    if item.price is not None and item.price > 0:
        return item.price
    return row.price if row.price is not None else row.stop_price


def fuzzy_match(
    row: OrderRow, items: list[VenueOrderRow], *, claimed_order_nos: set[str]
) -> VenueOrderRow | None:
    """ADR §B lost-ack match: (symbol, side, qty) within ±5 s; only a UNIQUE candidate counts."""
    candidates = []
    for item in items:
        if item.order_no in claimed_order_nos:
            continue
        if item.symbol != row.symbol or item.volume != row.quantity:
            continue
        if from_venue_side(item.side) is not row.side:
            continue
        if item.entry_time is None:
            continue
        if abs((item.entry_time - row.created_at).total_seconds()) <= _FUZZY_WINDOW_SECONDS:
            candidates.append(item)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "reconcile: ambiguous lost-ack fuzzy match for %s (%d) — skipped",
            row.client_order_id,
            len(candidates),
        )
    return None


def _venue_terminal_actions(row: OrderRow, item: VenueOrderRow) -> list[ReconcileAction]:
    state = classify_venue_state(item)
    cid = row.client_order_id
    if state is VenueOrderState.CANCELLED:
        return [ReconcileAction(kind="cancel_two_step", client_order_id=cid)]
    if state is VenueOrderState.EXPIRED:
        return [ReconcileAction(kind="expire", client_order_id=cid)]
    if state is VenueOrderState.REJECTED:
        reason = (
            f"streaming_pro reject {item.reject_reason or item.status_show or item.status}".strip()
        )
        if row.status is OrderState.PENDING_NEW:
            return [ReconcileAction(kind="reject", client_order_id=cid, reason=reason)]
        return [ReconcileAction(kind="post_ack_reject", client_order_id=cid, reason=reason)]
    return []


def plan_actions(
    row: OrderRow, filled_qty: int, item: VenueOrderRow | None, *, now: datetime
) -> list[ReconcileAction]:
    """Pure planning: one local row + its venue counterpart → persistence steps."""
    cid = row.client_order_id
    age_seconds = (now - row.created_at).total_seconds()

    if item is None:
        if row.status is OrderState.PENDING_NEW:
            if age_seconds > _ACK_LOST_TIMEOUT_SECONDS:
                return [
                    ReconcileAction(kind="reject", client_order_id=cid, reason="ack_lost_unmatched")
                ]
            return []  # still inside the lost-ack window — wait, never re-send
        if row.status is OrderState.PENDING_CANCEL:
            return [ReconcileAction(kind="cancel_confirm", client_order_id=cid)]
        if age_seconds > _ACK_LOST_TIMEOUT_SECONDS:
            logger.warning(
                "reconcile: %s (%s) missing from the venue book — drift, no transition (v1)",
                cid,
                row.status,
            )
        return []

    actions: list[ReconcileAction] = []
    if row.status is OrderState.PENDING_NEW:
        terminal = _venue_terminal_actions(row, item)
        if terminal and terminal[0].kind == "reject":
            return terminal
        actions.append(
            ReconcileAction(kind="ack", client_order_id=cid, broker_order_id=item.order_no)
        )

    if row.status is OrderState.PENDING_CANCEL:
        state = classify_venue_state(item)
        if state in (VenueOrderState.CANCELLED, VenueOrderState.EXPIRED):
            return [ReconcileAction(kind="cancel_confirm", client_order_id=cid)]
        if item.matched > filled_qty:
            logger.warning(
                "reconcile: %s has %d unrecorded matched while PENDING_CANCEL (v1 skip)",
                cid,
                item.matched - filled_qty,
            )
        return []

    delta = item.matched - filled_qty
    if delta > 0:
        price = fill_price_for(row, item)
        if price is None:
            logger.warning("reconcile: %s matched delta without a derivable price — skipped", cid)
        else:
            actions.append(
                ReconcileAction(
                    kind="fill",
                    client_order_id=cid,
                    fill_qty=delta,
                    fill_price=price,
                    fill_id=f"{item.order_no}:{item.matched}",
                    total_quantity=row.quantity,
                )
            )

    if item.matched < row.quantity:
        actions.extend(_venue_terminal_actions(row, item))
    return actions


class StreamingProReconciler:
    """The asyncio background worker (started only at micro_live/live)."""

    def __init__(
        self,
        adapter: StreamingProAdapter,
        *,
        interval_seconds: int,
        pool_provider: Callable[[], asyncpg.Pool],
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._adapter = adapter
        self._interval_seconds = interval_seconds
        self._pool_provider = pool_provider
        self._now = now

    async def reconcile_once(self) -> int:
        """One full pass; returns the number of actions applied (observability)."""
        pool = self._pool_provider()
        rows = await repositories.fetch_orders_for_reconcile(pool, Broker.STREAMING_PRO)
        if not rows:
            return 0
        now = self._now()
        by_group: dict[tuple[str, Market], list[OrderRow]] = {}
        for row in rows:
            by_group.setdefault((row.account, row.market), []).append(row)

        applied = 0
        for (account, market), group_rows in by_group.items():
            try:
                items = await self._adapter.fetch_venue_orders(account, market)
            except StreamingProTransportError as exc:
                logger.warning("reconcile: venue orders unavailable, group skipped: %s", exc)
                continue
            index = {item.order_no: item for item in items}
            claimed = {r.broker_order_id for r in group_rows if r.broker_order_id is not None}
            for row in group_rows:
                item = index.get(row.broker_order_id) if row.broker_order_id else None
                if (
                    item is None
                    and row.status is OrderState.PENDING_NEW
                    and (now - row.created_at).total_seconds() > _STUCK_PENDING_SECONDS
                ):
                    item = fuzzy_match(row, items, claimed_order_nos=claimed)
                    if item is not None:
                        claimed.add(item.order_no)
                result = await repositories.fetch_order_result(pool, row.client_order_id)
                filled_qty = result.filled_qty if result is not None else 0
                for action in plan_actions(row, filled_qty, item, now=now):
                    try:
                        await self._execute(pool, action)
                        applied += 1
                    except Exception:  # noqa: BLE001 - one bad row never stops the pass
                        logger.exception(
                            "reconcile: action %s failed for %s",
                            action.kind,
                            action.client_order_id,
                        )
        return applied

    async def _execute(self, pool: asyncpg.Pool, action: ReconcileAction) -> None:
        cid = action.client_order_id
        if action.kind == "ack":
            assert action.broker_order_id is not None
            await repositories.ack_order(pool, cid, action.broker_order_id)
            logger.info("reconcile: %s acked from venue truth", cid)
        elif action.kind == "fill":
            assert action.fill_id is not None and action.fill_price is not None
            await repositories.apply_fill(
                pool,
                cid,
                broker_fill_id=action.fill_id,
                price=action.fill_price,
                quantity=action.fill_qty,
                exec_ts=self._now(),
                total_quantity=action.total_quantity,
            )
        elif action.kind == "cancel_two_step":
            await repositories.update_status(pool, cid, OrderState.PENDING_CANCEL)
            await repositories.update_status(pool, cid, OrderState.CANCELLED)
        elif action.kind == "cancel_confirm":
            await repositories.update_status(pool, cid, OrderState.CANCELLED)
        elif action.kind == "expire":
            await repositories.update_status(pool, cid, OrderState.EXPIRED)
        elif action.kind == "reject":
            await repositories.update_status(pool, cid, OrderState.REJECTED)
            await repositories.set_reject_reason(pool, cid, action.reason or "rejected at venue")
        elif action.kind == "post_ack_reject":
            logger.warning("reconcile: %s rejected at venue AFTER ack — closing as CANCELLED", cid)
            await repositories.set_reject_reason(pool, cid, action.reason or "rejected at venue")
            await repositories.update_status(pool, cid, OrderState.PENDING_CANCEL)
            await repositories.update_status(pool, cid, OrderState.CANCELLED)
        else:  # pragma: no cover - planning never emits unknown kinds
            raise ValueError(f"unknown reconcile action kind: {action.kind}")

    async def run(self) -> None:
        """The background worker shell (started by the runtime)."""
        while True:  # pragma: no branch - cancelled via task.cancel()
            try:
                await self.reconcile_once()
            except Exception:  # noqa: BLE001 - the loop never dies
                logger.exception("reconcile pass failed unexpectedly")
            await asyncio.sleep(self._interval_seconds)

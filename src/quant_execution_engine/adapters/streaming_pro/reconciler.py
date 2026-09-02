"""Reconciliation loop v1 — repairs submit/ack drift against venue truth (§B).

Mirrors the Liberator reconciler: every pass polls ``GET /orders`` per ``(account, market)`` with
non-terminal streaming_pro rows and drives the frozen 13-edge machine through the Phase-2 repository
seams (``ack_order`` / ``apply_fill`` / ``update_status``):

* lost ack: a ``PENDING_NEW`` stuck >5 s fuzzy-matches ``(symbol, side, qty, **price**)`` with
  venue ``entryTime`` within ±5 s of the persisted submit ts; a unique match acks, and a row with
  **no** candidate past 60 s resolves bounded to ``REJECTED`` — the loop NEVER re-sends.
  🔴 An **AMBIGUOUS** match is skipped and the row **stays PENDING_NEW indefinitely**, never
  rejected: candidates existing means the order is almost certainly live at the venue. Treating
  ambiguity as absence wrote terminal REJECTEDs for live real-money orders on the liberator twin
  (2026-09-02, [[TK-0493]]); this adapter carried the identical defect, unfired;
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


def _has_price(value: Decimal | None) -> bool:
    """A usable price for matching: present AND positive.

    ``0`` means *no price reported* here — the same convention ``fill_price_for`` above
    already applies. Treating it as a real value would mismatch every priced candidate,
    excluding all of them; the caller reads zero candidates as *absent* and rejects at
    the timeout, which is the [[TK-0493]] bug by another route.
    """
    return value is not None and value > 0


@dataclass(frozen=True)
class MatchOutcome:
    """Why :func:`fuzzy_match` returned what it did — see the liberator twin.

    🔴 *"Nothing resembles this order"* and *"several do and I declined to choose"* are
    different facts. Collapsing both into a bare ``None`` let the caller write a TERMINAL
    ``REJECTED`` for orders resting live at the venue ([[TK-0493]], issue #270).
    """

    item: VenueOrderRow | None
    candidate_count: int

    @property
    def ambiguous(self) -> bool:
        return self.item is None and self.candidate_count > 1


def fuzzy_match(
    row: OrderRow, items: list[VenueOrderRow], *, claimed_order_nos: set[str]
) -> MatchOutcome:
    """ADR §B lost-ack match: (symbol, side, qty, **price**) within ±5 s.

    Only a UNIQUE candidate counts.

    🔑 **Price joined the key on 2026-09-02.** This adapter had the identical omission to
    liberator's, where it produced four false terminal REJECTEDs on live real-money orders.
    It had never fired here only because no *laddered* streaming_pro order had been placed —
    fixing liberator alone would have left a loaded copy. A price ladder (same symbol, side
    and qty, seconds apart, differing only in price) is the input class the old key was
    blind to.

    ⛔ Venue rows that are already terminal are deliberately NOT excluded: our lost-ack
    order may itself be the terminal one, and excluding them could leave one candidate that
    is a *different, live* order. A confident wrong match is worse than an ambiguous one.
    """
    candidates = []
    for item in items:
        if item.order_no in claimed_order_nos:
            continue
        if item.symbol != row.symbol or item.volume != row.quantity:
            continue
        if from_venue_side(item.side) is not row.side:
            continue
        if _has_price(item.price) and _has_price(row.price) and item.price != row.price:
            continue
        if item.entry_time is None:
            continue
        if abs((item.entry_time - row.created_at).total_seconds()) <= _FUZZY_WINDOW_SECONDS:
            candidates.append(item)
    if len(candidates) == 1:
        return MatchOutcome(item=candidates[0], candidate_count=1)
    if len(candidates) > 1:
        logger.warning(
            "reconcile: ambiguous lost-ack fuzzy match for %s (%d) — skipped; the order is "
            "LIVE at the venue and will NOT be marked terminal",
            row.client_order_id,
            len(candidates),
        )
    return MatchOutcome(item=None, candidate_count=len(candidates))


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
    row: OrderRow,
    filled_qty: int,
    item: VenueOrderRow | None,
    *,
    now: datetime,
    ambiguous: bool = False,
) -> list[ReconcileAction]:
    """Pure planning: one local row + its venue counterpart → persistence steps.

    ``ambiguous`` carries :attr:`MatchOutcome.ambiguous`. It defaults to ``False`` — the
    truthful value for any caller that ran no lost-ack match — and the production loop
    passes it explicitly, with a test asserting that wiring rather than trusting it.
    """
    cid = row.client_order_id
    age_seconds = (now - row.created_at).total_seconds()

    if item is None:
        if row.status is OrderState.PENDING_NEW:
            # 🔴 AMBIGUITY IS NOT ABSENCE — see the liberator twin and [[TK-0493]].
            # Candidates existed, so one of them is almost certainly this order, live at
            # the venue. The row stays PENDING_NEW: the honest state, and the only one
            # available (the 9-state machine is frozen and a DB CHECK enforces it).
            if ambiguous:
                if age_seconds > _ACK_LOST_TIMEOUT_SECONDS:
                    logger.error(
                        "reconcile: %s has been AMBIGUOUS for %.0fs (past the %ds mark where "
                        "it used to be wrongly REJECTED). The order is LIVE at the venue and "
                        "stays PENDING_NEW. Resolve by venue orderNo; NEVER resubmit.",
                        cid,
                        age_seconds,
                        _ACK_LOST_TIMEOUT_SECONDS,
                    )
                return []
            if age_seconds > _ACK_LOST_TIMEOUT_SECONDS:
                return [
                    ReconcileAction(kind="reject", client_order_id=cid, reason="ack_lost_unmatched")
                ]
            return []  # still inside the lost-ack window — wait, never re-send
        if row.status is OrderState.PENDING_CANCEL:
            # ⚠️ [[TK-0459]] — this line makes an ASSUMPTION nobody has verified: that
            # absence from the SP order book means the cancel landed.
            #
            # The Liberator reconciler carried the identical inference and it was WRONG
            # across a day boundary — `/va/order` is current-day-only, so from the next
            # day every order is absent by construction and this would confirm a terminal
            # CANCELLED on no evidence ([[TK-0446]], fixed and deployed 2026-08-28).
            #
            # 🔴 The same guard is deliberately NOT applied here, because whether SP's
            # `.../accounts/{account}/orders` is day-scoped is UNKNOWN. If it is not,
            # adding the guard would stop confirming legitimate cancels and strand rows
            # non-terminal — the guard would be the regression.
            #
            # Three routes were tried and all are exhausted (2026-08-28): the bridge
            # source and reference docs say nothing about scope; there is no separate
            # order-history endpoint whose existence would imply this one is current-only;
            # and the empirical check is BLOCKED because no SP order has ever reached the
            # venue — the two `broker=streaming_pro` rows in the store carry `SIM-` handles
            # from paper-stage interception, so the empty book is uninterpretable.
            #
            # What would settle it: a real SP order that survives to a later day, read back
            # with a positive control in the same request. Operator-only.
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
                # Per-row, NEVER hoisted: a value surviving into the next iteration would
                # suppress a legitimate rejection for a DIFFERENT order.
                ambiguous = False
                if (
                    item is None
                    and row.status is OrderState.PENDING_NEW
                    and (now - row.created_at).total_seconds() > _STUCK_PENDING_SECONDS
                ):
                    outcome = fuzzy_match(row, items, claimed_order_nos=claimed)
                    item = outcome.item
                    ambiguous = outcome.ambiguous
                    if item is not None:
                        claimed.add(item.order_no)
                result = await repositories.fetch_order_result(pool, row.client_order_id)
                filled_qty = result.filled_qty if result is not None else 0
                for action in plan_actions(row, filled_qty, item, now=now, ambiguous=ambiguous):
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

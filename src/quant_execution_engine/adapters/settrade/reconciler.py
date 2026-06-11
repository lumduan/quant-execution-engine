"""Reconciliation loop v1 — repairs submit/ack drift against venue truth (§B).

A verbatim structural mirror of ``adapters/liberator/reconciler.py`` (Design
Decision 7: mirror-not-abstract; a shared base is deferred to Phase 6) with the
Settrade deltas: polling is grouped per ``(account, market)`` (two books, both
account-scoped), the GET rate budget is consulted before each group, and a new
``replace_resolve`` action repairs a stranded ``PENDING_REPLACE`` (crash or
lost response mid-amend).

Every pass polls ``GET .../orders`` for each ``(account, market)`` group with
non-terminal Settrade rows and drives the frozen 13-edge state machine through
the Phase-2 repository seams (``ack_order`` / ``apply_fill`` / ``update_status``
/ ``replace_order``):

* lost ack: a ``PENDING_NEW`` stuck >5 s fuzzy-matches ``(account, symbol,
  side, qty)`` with venue ``entryTime`` within ±5 s of the persisted submit
  timestamp; a unique match acks, an ambiguous one is skipped (never guessed),
  and an unmatched row past 60 s resolves bounded to ``REJECTED
  ("ack_lost_unmatched")`` — the loop NEVER re-sends;
* fills: venue ``matched`` is cumulative — the delta against the durable fill
  aggregate becomes one fill with the deterministic id ``{orderNo}:{matched}``
  (re-polls dedupe via the existing ``ON CONFLICT DO NOTHING``);
* PENDING_REPLACE (native amend stranded): venue resting → one-statement
  ``replace_order`` with venue-truth price/qty; venue terminal → resolve then
  legal close-out; venue missing → restore local values (COALESCE keeps them);
* venue terminals map onto legal edges only: cancelled → PENDING_CANCEL →
  CANCELLED (two-step), expired → EXPIRED, a post-ack venue reject (no
  ``NEW→REJECTED`` edge exists) → reject_reason + PENDING_CANCEL → CANCELLED.

``plan_actions`` is a pure function — the parametrized-test surface; the loop
shell around it stays thin. Account numbers are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg

from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
from src.quant_execution_engine.adapters.settrade.errors import (
    SettradeMarketNotConfigured,
    SettradeVenueRejection,
)
from src.quant_execution_engine.adapters.settrade.mapping import (
    VenueOrderState,
    classify_venue_state,
    from_venue_side,
)
from src.quant_execution_engine.adapters.settrade.models import SettradeOrderItem
from src.quant_execution_engine.contracts.enums import Broker, Market, OrderState
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.models import OrderRow

logger = logging.getLogger(__name__)

_STUCK_PENDING_SECONDS = 5.0  # ADR §B: lost-ack window opens
_ACK_LOST_TIMEOUT_SECONDS = 60.0  # bounded resolution (~5 passes at the 12 s default)
_FUZZY_WINDOW_SECONDS = 5.0  # ADR §B: ±5 s around the persisted submit ts
_PRICE_QUANTUM = Decimal("0.000001")  # numeric(18,6) — the DB price precision


@dataclass(frozen=True)
class ReconcileAction:
    """One persistence step the executor applies (smallest testable unit)."""

    kind: str  # ack | fill | cancel_two_step | cancel_confirm | expire | reject
    # | post_ack_reject | replace_resolve
    client_order_id: str
    broker_order_id: str | None = None
    fill_qty: int = 0
    fill_price: Decimal | None = None
    fill_id: str | None = None
    total_quantity: int = 0
    reason: str | None = None
    # replace_resolve carries venue-truth price/qty (None = restore local values).
    replace_price: Decimal | None = None
    replace_qty: int | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def fill_price_for(row: OrderRow, item: SettradeOrderItem) -> Decimal | None:
    """v1 fill-price source: venue order price, else local price/stop.

    A per-fill price stream does not exist until Phase 5 (MQTT) — this is the
    documented approximation (decision log, Phase 4). Settrade order items carry
    no cumulative notional, so there is no average-price middle rung.
    """
    if item.price is not None and item.price > 0:
        return item.price.quantize(_PRICE_QUANTUM)
    return row.price if row.price is not None else row.stop_price


def fuzzy_match(
    row: OrderRow,
    items: list[SettradeOrderItem],
    *,
    claimed_order_nos: set[str],
) -> SettradeOrderItem | None:
    """ADR §B lost-ack match: (account, symbol, side, qty) within ±5 s.

    Only a UNIQUE candidate counts — an ambiguous match is skipped with a
    warning, never guessed. Venue rows already claimed by another local order
    are excluded.
    """
    candidates = []
    for item in items:
        if item.order_no in claimed_order_nos:
            continue
        if item.symbol != row.symbol or item.quantity != row.quantity:
            continue
        if from_venue_side(item.side) is not row.side:
            continue
        if item.entry_time is None:
            continue
        skew = abs((item.entry_time - row.created_at).total_seconds())
        if skew <= _FUZZY_WINDOW_SECONDS:
            candidates.append(item)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "reconcile: ambiguous lost-ack fuzzy match for %s (%d candidates) — skipped",
            row.client_order_id,
            len(candidates),
        )
    return None


def _venue_terminal_actions(row: OrderRow, item: SettradeOrderItem) -> list[ReconcileAction]:
    """Map a venue terminal classification onto frozen-legal edges."""
    state = classify_venue_state(item)
    cid = row.client_order_id
    if state is VenueOrderState.CANCELLED:
        return [ReconcileAction(kind="cancel_two_step", client_order_id=cid)]
    if state is VenueOrderState.EXPIRED:
        return [ReconcileAction(kind="expire", client_order_id=cid)]
    if state is VenueOrderState.REJECTED:
        reason = f"settrade reject {item.reject_code or item.reject_reason or item.status}".strip()
        if row.status is OrderState.PENDING_NEW:
            return [ReconcileAction(kind="reject", client_order_id=cid, reason=reason)]
        # No NEW->REJECTED edge: persist the venue truth in reject_reason and
        # close out through the legal cancel path (decision log, Phase 4).
        return [ReconcileAction(kind="post_ack_reject", client_order_id=cid, reason=reason)]
    return []


def _plan_pending_replace(row: OrderRow, item: SettradeOrderItem | None) -> list[ReconcileAction]:
    """Resolve a stranded PENDING_REPLACE (crash/lost response mid-amend).

    Venue item missing → restore local price/qty (COALESCE keeps them). Venue
    item resting → restore NEW with venue-truth price/qty. Venue item terminal →
    restore NEW first (the executor applies in order — PENDING_REPLACE→NEW is the
    only legal exit), then the terminal close-out on top.
    """
    cid = row.client_order_id
    if item is None:
        return [ReconcileAction(kind="replace_resolve", client_order_id=cid)]
    if classify_venue_state(item) is VenueOrderState.RESTING:
        return [
            ReconcileAction(
                kind="replace_resolve",
                client_order_id=cid,
                replace_price=item.price,
                replace_qty=item.quantity or None,
            )
        ]
    # Venue terminal: resolve to NEW first, then the legal close-out actions.
    return [
        ReconcileAction(
            kind="replace_resolve",
            client_order_id=cid,
            replace_price=item.price,
            replace_qty=item.quantity or None,
        ),
        *_venue_terminal_actions(row, item),
    ]


def plan_actions(
    row: OrderRow,
    filled_qty: int,
    item: SettradeOrderItem | None,
    *,
    now: datetime,
) -> list[ReconcileAction]:
    """Pure planning: one local row + its venue counterpart → persistence steps."""
    cid = row.client_order_id
    age_seconds = (now - row.created_at).total_seconds()

    if row.status is OrderState.PENDING_REPLACE:
        return _plan_pending_replace(row, item)

    if item is None:
        if row.status is OrderState.PENDING_NEW:
            if age_seconds > _ACK_LOST_TIMEOUT_SECONDS:
                return [
                    ReconcileAction(kind="reject", client_order_id=cid, reason="ack_lost_unmatched")
                ]
            return []  # still inside the lost-ack window — wait, never re-send
        if row.status is OrderState.PENDING_CANCEL:
            # Absent from the venue book after a cancel request = gone.
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
            return terminal  # PENDING_NEW -> REJECTED directly; never ack first
        actions.append(
            ReconcileAction(kind="ack", client_order_id=cid, broker_order_id=item.order_no)
        )

    if row.status is OrderState.PENDING_CANCEL:
        state = classify_venue_state(item)
        if state in (VenueOrderState.CANCELLED, VenueOrderState.EXPIRED):
            return [ReconcileAction(kind="cancel_confirm", client_order_id=cid)]
        if item.matched > filled_qty:
            # PENDING_CANCEL -> PARTIALLY_FILLED is not a frozen edge; the
            # cancel confirm wins and the late fill is surfaced, not persisted.
            logger.warning(
                "reconcile: %s has %d unrecorded matched while PENDING_CANCEL (v1 skip)",
                cid,
                item.matched - filled_qty,
            )
        return []  # cancel not yet effective at the venue — wait

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
        # A fully-matched order ends FILLED via the fill action; otherwise a
        # venue cancel/expire (incl. one seen in the same poll as the ack)
        # still applies on top.
        actions.extend(_venue_terminal_actions(row, item))
    return actions


class SettradeReconciler:
    """The asyncio background worker (started only at micro_live/live)."""

    def __init__(
        self,
        adapter: SettradeAdapter,
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
        rows = await repositories.fetch_orders_for_reconcile(
            pool, Broker.SETTRADE, include_pending_replace=True
        )
        if not rows:
            return 0
        now = self._now()
        by_group: dict[tuple[str, Market], list[OrderRow]] = {}
        for row in rows:
            by_group.setdefault((row.account, row.market), []).append(row)

        applied = 0
        # Per-market budget skip (Phase 4.1): one market's exhausted GET bucket
        # must NOT starve the other market's groups (the old whole-pass break
        # inverted starvation). Warn once per exhausted market per pass.
        exhausted: set[Market] = set()
        warned: set[Market] = set()
        for (account, market), group_rows in by_group.items():
            if market in exhausted or self._adapter.get_budget_exhausted(market):
                exhausted.add(market)
                if market not in warned:
                    logger.warning(
                        "reconcile: settrade %s GET budget exhausted — skipping its groups",
                        market.value,
                    )
                    warned.add(market)
                continue
            try:
                items = await self._adapter.fetch_venue_orders(account, market)
            except SettradeVenueRejection as exc:
                logger.warning(
                    "reconcile: %s venue orders unavailable, group skipped: %s",
                    market,
                    exc.venue_code,
                )
                continue
            except SettradeMarketNotConfigured as exc:
                # No app for this market — rows freeze and nag (never faked truth).
                logger.warning(
                    "reconcile: %s venue orders unavailable, group skipped: %s", market, exc
                )
                continue
            applied += await self._reconcile_group(pool, group_rows, items, now=now)
        return applied

    async def _reconcile_group(
        self,
        pool: asyncpg.Pool,
        group_rows: list[OrderRow],
        items: list[SettradeOrderItem],
        *,
        now: datetime,
    ) -> int:
        index = {item.order_no: item for item in items}
        claimed = {r.broker_order_id for r in group_rows if r.broker_order_id is not None}
        applied = 0
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
                    await self._execute(pool, action, filled_qty=filled_qty)
                    applied += 1
                except Exception:  # noqa: BLE001 - one bad row never stops the pass
                    logger.exception(
                        "reconcile: action %s failed for %s",
                        action.kind,
                        action.client_order_id,
                    )
        return applied

    async def _execute(
        self, pool: asyncpg.Pool, action: ReconcileAction, *, filled_qty: int
    ) -> None:
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
        elif action.kind == "replace_resolve":
            await repositories.replace_order(pool, cid, action.replace_price, action.replace_qty)
            if filled_qty > 0:
                await repositories.update_status(pool, cid, OrderState.PARTIALLY_FILLED)
            logger.info("reconcile: %s PENDING_REPLACE resolved to venue truth", cid)
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

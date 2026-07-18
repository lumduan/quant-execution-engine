"""``OrderRouter`` — the submit/cancel/get orchestration over the frozen rules.

Pipeline order (reconciled in the Phase 2 plan): kill-switch FIRST (hard rule
3 — precedes even dedupe), then the frozen validation sequence: dedupe →
capability gate → PTRM → stage gate → venue-class hook (no-op Phase 2) →
single-flight insert + place. A duplicate ``client_order_id`` is never an
error: it returns the prior result (ADR §A).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg

from src.quant_execution_engine.adapters.base import AmendAck, BrokerAdapter
from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.adapters.market_data import MarketDataClient
from src.quant_execution_engine.adapters.sim import FillPriceSource, SimAdapter
from src.quant_execution_engine.cache.single_flight import single_flight
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts import capabilities
from src.quant_execution_engine.contracts.enums import Broker, OrderState, to_public_status
from src.quant_execution_engine.contracts.errors import (
    AmendRejected,
    ConcurrentSubmit,
    IllegalTransition,
    OrderNotFound,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder, NormalizedOrderResult
from src.quant_execution_engine.core import state_machine
from src.quant_execution_engine.core.kill_switch import KillSwitch
from src.quant_execution_engine.core.price_band import PriceBandCheck
from src.quant_execution_engine.core.risk import RiskGate
from src.quant_execution_engine.core.stage import AdapterIntent, resolve_adapter
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.errors import DuplicateOrderSignal
from src.quant_execution_engine.db.models import OrderResultRow, OrderRow

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class SubmitOutcome:
    """The result plus whether it was served from the dedupe path."""

    result: NormalizedOrderResult
    duplicate: bool


def _amended_order(
    row: OrderRow,
    client_order_id: str,
    *,
    new_price: Decimal | None,
    new_qty: int | None,
) -> NormalizedOrder:
    """Rebuild a NormalizedOrder from a stored row with overridden price/qty.

    Shared by both amend branches: native re-uses the same ``client_order_id``,
    cancel_replace supplies the fresh replacement id.
    """
    return NormalizedOrder(
        client_order_id=client_order_id,
        broker=row.broker,
        account=row.account,
        market=row.market,
        symbol=row.symbol,
        side=row.side,
        order_type=row.order_type,
        price=new_price if new_price is not None else row.price,
        stop_price=row.stop_price,
        quantity=new_qty if new_qty is not None else row.quantity,
        display_qty=row.display_qty,
        tif=row.tif,
        position_effect=row.position_effect,
    )


class OrderRouter:
    """Per-request orchestrator (cheap to build; backed by process singletons)."""

    def __init__(
        self,
        *,
        settings: Settings,
        pool: asyncpg.Pool,
        redis: Any | None,
        liberator_adapter: BrokerAdapter | None = None,
        streaming_pro_adapter: BrokerAdapter | None = None,
        sim_price_source: FillPriceSource | None = None,
        market_data_client: MarketDataClient | None = None,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._redis = redis
        self._sim = SimAdapter(
            default_fill_price=settings.sim_default_fill_price,
            price_source=sim_price_source,
        )
        # Injected process singletons (api/deps.py / runtime); None = not configured.
        self._liberator = liberator_adapter
        self._streaming_pro = streaming_pro_adapter
        self._risk = RiskGate(settings, redis)
        # The price-band check (A2) is advisory + default-off: with no market-data
        # client injected it runs against an unconfigured client, so .enabled is
        # False and check() is a no-op. The shared singleton is borrowed, not owned.
        self._price_band = PriceBandCheck(
            settings, market_data_client or MarketDataClient(None, None)
        )
        self.kill_switch = KillSwitch(settings, redis)

    def _resolve_adapter(
        self,
        broker: Broker,
        *,
        intent: AdapterIntent = AdapterIntent.TRADE,
    ) -> BrokerAdapter:
        """Thread every injected adapter through the stage ladder (one call site)."""
        return resolve_adapter(
            self._settings.stage,
            broker,
            sim_adapter=self._sim,
            liberator_adapter=self._liberator,
            streaming_pro_adapter=self._streaming_pro,
            intent=intent,
        )

    # ------------------------------------------------------------------ submit
    async def submit(self, order: NormalizedOrder, strategy_id: str | None = None) -> SubmitOutcome:
        """The full frozen submit pipeline.

        ``strategy_id`` (the ``X-Strategy-Id`` header, D16) is persisted with the
        order and echoed on every order-update event — it is transport metadata,
        not part of the frozen ``NormalizedOrder`` contract, so it threads
        alongside the order, never inside it.
        """
        await self.kill_switch.assert_disengaged()  # FIRST (hard rule 3)

        existing = await repositories.fetch_order_result(self._pool, order.client_order_id)
        if existing is not None:
            return SubmitOutcome(result=self._to_result(existing), duplicate=True)

        caps = capabilities.lookup(order.broker, order.market)
        caps.assert_supports(order.order_type, order.tif, order.position_effect)

        await self._risk.check(order)
        # Advisory price-band check (A2), AFTER the PTRM gate and BEFORE any
        # adapter routing — a capped/malformed order is rejected before any
        # market-data hop, and the kill-switch-first invariant is preserved.
        await self._price_band.check(order)

        adapter = self._resolve_adapter(order.broker)
        adapter.breaker.guard()
        # Venue-class field validation hook: per-(broker, market, order_type)
        # strict rules plug in here in Phases 3/4 (ADR §G); no-op in Phase 2.

        lock_key = f"exe:submit:{order.client_order_id}"
        async with single_flight(
            self._redis, lock_key, ttl_seconds=self._settings.submit_lock_ttl_seconds
        ) as acquired:
            if not acquired:
                return await self._await_concurrent(order.client_order_id)
            try:
                await repositories.insert_order(self._pool, order, strategy_id)
            except DuplicateOrderSignal:
                row = await repositories.fetch_order_result(self._pool, order.client_order_id)
                if row is None:  # pragma: no cover - PK fired, row must exist
                    raise ConcurrentSubmit(
                        "submit raced and no durable row is visible yet",
                        client_order_id=order.client_order_id,
                    ) from None
                return SubmitOutcome(result=self._to_result(row), duplicate=True)
            await self._place_and_settle(adapter, order)

        row = await repositories.fetch_order_result(self._pool, order.client_order_id)
        if row is None:  # pragma: no cover - we just inserted it
            raise OrderNotFound("order vanished mid-submit", client_order_id=order.client_order_id)
        return SubmitOutcome(result=self._to_result(row), duplicate=False)

    async def _place_and_settle(self, adapter: BrokerAdapter, order: NormalizedOrder) -> None:
        """Route to the adapter and persist the resulting lifecycle."""
        ack = await adapter.place(order)
        if ack.rejected:
            await repositories.update_status(self._pool, order.client_order_id, OrderState.REJECTED)
            await repositories.set_reject_reason(
                self._pool, order.client_order_id, ack.reject_reason or "rejected by adapter"
            )
            return
        if ack.broker_order_id is None:  # pragma: no cover - adapter contract
            raise AdapterError("adapter ack carried no broker_order_id")
        # ONE statement: status -> NEW + broker_order_id (§B atomic; trigger audits).
        await repositories.ack_order(self._pool, order.client_order_id, ack.broker_order_id)
        for fill in ack.fills:
            await repositories.apply_fill(
                self._pool,
                order.client_order_id,
                broker_fill_id=fill.broker_fill_id,
                price=fill.price,
                quantity=fill.quantity,
                exec_ts=fill.exec_ts,
                total_quantity=order.quantity,
            )
        if ack.remainder_cancelled:
            await repositories.update_status(
                self._pool, order.client_order_id, OrderState.PENDING_CANCEL
            )
            await adapter.cancel(order.client_order_id)
            await repositories.update_status(
                self._pool, order.client_order_id, OrderState.CANCELLED
            )

    async def _await_concurrent(self, client_order_id: str) -> SubmitOutcome:
        """Lock-miss: an identical submit is mid-flight — wait briefly for its row."""
        budget = self._settings.submit_lock_wait_ms / 1000.0
        waited = 0.0
        while True:
            row = await repositories.fetch_order_result(self._pool, client_order_id)
            if row is not None:
                return SubmitOutcome(result=self._to_result(row), duplicate=True)
            if waited >= budget:
                raise ConcurrentSubmit(
                    "an identical submit is in flight; retry shortly",
                    client_order_id=client_order_id,
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            waited += _POLL_INTERVAL_SECONDS

    # ------------------------------------------------------------------ cancel
    async def cancel(self, client_order_id: str) -> NormalizedOrderResult:
        """NEW/PARTIALLY_FILLED -> PENDING_CANCEL -> CANCELLED (frozen edges only).

        Deliberately NOT blocked by the kill-switch: cancels reduce risk and
        the mass-cancel sweep uses this same path.
        """
        row = await repositories.fetch_order(self._pool, client_order_id)
        if row is None:
            raise OrderNotFound("unknown client_order_id", client_order_id=client_order_id)
        if state_machine.is_terminal(row.status):
            raise IllegalTransition(
                f"order is already terminal ({row.status})", client_order_id=client_order_id
            )
        if row.status is OrderState.PENDING_CANCEL:
            return await self.get(client_order_id)  # idempotent re-cancel
        if row.status in (OrderState.PENDING_NEW, OrderState.PENDING_REPLACE):
            raise IllegalTransition(
                f"no frozen cancel edge from {row.status} (transient pending state)",
                client_order_id=client_order_id,
            )
        adapter = self._resolve_adapter(row.broker)
        adapter.breaker.guard()
        await repositories.update_status(self._pool, client_order_id, OrderState.PENDING_CANCEL)
        ack = await adapter.cancel(client_order_id)
        if not ack.ok:
            # Stuck PENDING_CANCEL resolves via reconciliation (Phase 3); never silent.
            raise AdapterError(f"cancel not confirmed by adapter: {ack.reason}")
        await repositories.update_status(self._pool, client_order_id, OrderState.CANCELLED)
        return await self.get(client_order_id)

    # ------------------------------------------------------------------- amend
    async def amend(
        self,
        client_order_id: str,
        *,
        new_client_order_id: str | None = None,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> SubmitOutcome:
        """Amend price/qty; branches on the order's declared amend semantics.

        The branch key is the ORDER's ``capabilities.lookup(broker, market).amend``:
        ``native`` (sim) amends in place via PENDING_REPLACE; ``cancel_replace``
        (Liberator + Streaming Pro, which have no amend route, R2) cancels then
        resubmits. After the broker-023 removal no REAL broker declares ``native``
        — the in-place path is exercised only by the ``sim`` broker.

        Kill-switch FIRST (asymmetric with the un-gated cancel path): amends can
        *increase* exposure, so the switch precedes everything in the amend path
        — a cancel reduces risk and stays un-gated, an amend must not slip a
        larger order past an engaged kill-switch.
        """
        await self.kill_switch.assert_disengaged()  # FIRST — amends can raise exposure
        if new_price is None and new_qty is None:
            raise AmendRejected(
                "amend requires new_price and/or new_qty", client_order_id=client_order_id
            )
        row = await repositories.fetch_order(self._pool, client_order_id)
        if row is None:
            raise OrderNotFound("unknown client_order_id", client_order_id=client_order_id)
        semantics = capabilities.lookup(row.broker, row.market).amend
        if semantics == "native":
            return await self._amend_native(
                row, new_client_order_id, new_price=new_price, new_qty=new_qty
            )
        return await self._amend_cancel_replace(
            row, new_client_order_id, new_price=new_price, new_qty=new_qty
        )

    async def _amend_cancel_replace(
        self,
        row: OrderRow,
        new_client_order_id: str | None,
        *,
        new_price: Decimal | None,
        new_qty: int | None,
    ) -> SubmitOutcome:
        """Cancel+replace orchestration (Liberator has no amend route, R2).

        The old order ends CANCELLED via the frozen PENDING_CANCEL path
        (PENDING_REPLACE is reserved for native amends); the replacement enters
        the FULL frozen submit pipeline under the caller-supplied fresh
        ``client_order_id``. Declared consequences of ``cancel_replace``
        semantics: queue priority is lost and a brief no-resting-order window
        exists. Under an engaged kill-switch the amend is rejected up front (see
        :meth:`amend`). The replacement gets NO PTRM exemption: a price-only
        amend re-using the same quantity inside the duplicate-burst window is
        risk-rejected (old order already cancelled — flat, retry after window).
        """
        if new_client_order_id is None:
            raise AmendRejected(
                "cancel_replace amend requires new_client_order_id",
                client_order_id=row.client_order_id,
            )
        await self.cancel(row.client_order_id)
        replacement = _amended_order(row, new_client_order_id, new_price=new_price, new_qty=new_qty)
        # The replacement inherits the original's strategy attribution (D16) so
        # the stream keeps both legs under one strategy_id.
        return await self.submit(replacement, row.strategy_id)

    async def _amend_native(
        self,
        row: OrderRow,
        new_client_order_id: str | None,
        *,
        new_price: Decimal | None,
        new_qty: int | None,
    ) -> SubmitOutcome:
        """Native amend over the frozen PENDING_REPLACE -> NEW edge (sim only).

        The amend rides one atomic ``replace_order`` UPDATE (status + price +
        quantity in one statement, so the audit trigger snapshots the amended
        values). A venue amend-reject (e.g. a partial fill raced the amend) is a
        NON-terminal restore: PENDING_REPLACE -> NEW (+ PARTIALLY_FILLED when
        already partly filled), then ``AmendRejected`` (409). The order is still
        LIVE — ``reject_reason`` is deliberately NOT written (it would imply the
        order is dead); the two audit rows + the typed envelope are the evidence.
        """
        cid = row.client_order_id
        if new_client_order_id is not None:
            raise AmendRejected(
                "native amend keeps the same client_order_id; omit new_client_order_id",
                client_order_id=cid,
            )
        if row.status not in (OrderState.NEW, OrderState.PARTIALLY_FILLED):
            detail = (
                "amend already in flight"
                if row.status is OrderState.PENDING_REPLACE
                else f"no amend edge from {row.status}"
            )
            raise IllegalTransition(detail, client_order_id=cid)

        result = await repositories.fetch_order_result(self._pool, cid)
        if result is None:  # pragma: no cover - row was just fetched
            raise OrderNotFound("order vanished mid-amend", client_order_id=cid)
        filled_qty = result.filled_qty
        self._assert_amend_quantities(cid, row, new_qty, filled_qty)

        amended = _amended_order(row, cid, new_price=new_price, new_qty=new_qty)
        await self._risk.check(amended)  # NO exemption (E17): a reject leaves the original resting

        adapter = self._resolve_adapter(row.broker)
        adapter.breaker.guard()

        await repositories.update_status(self._pool, cid, OrderState.PENDING_REPLACE)
        ack = await self._venue_amend(adapter, cid, new_price=new_price, new_qty=new_qty)
        if ack.ok:
            await repositories.replace_order(self._pool, cid, new_price, new_qty)
            await self._restore_partial(cid, filled_qty)
            return SubmitOutcome(result=await self.get(cid), duplicate=False)
        # Venue amend-reject: restore non-terminally; the order is still live.
        await repositories.update_status(self._pool, cid, OrderState.NEW)
        await self._restore_partial(cid, filled_qty)
        logger.warning("native amend rejected for %s: %s", cid, ack.reason)
        raise AmendRejected(ack.reason or "amend rejected by venue", client_order_id=cid)

    @staticmethod
    def _assert_amend_quantities(
        cid: str,
        row: OrderRow,
        new_qty: int | None,
        filled_qty: int,
    ) -> None:
        """Pre-flight the DB column CHECKs (quantity>0, display_qty<=quantity)."""
        if new_qty is None:
            return
        if new_qty <= filled_qty:
            raise AmendRejected("new_qty <= filled quantity; cancel instead", client_order_id=cid)
        if row.display_qty is not None and new_qty < row.display_qty:
            raise AmendRejected(
                "new_qty < display_qty (would violate display_qty <= quantity)",
                client_order_id=cid,
            )

    async def _venue_amend(
        self,
        adapter: BrokerAdapter,
        cid: str,
        *,
        new_price: Decimal | None,
        new_qty: int | None,
    ) -> AmendAck:
        """Call the adapter amend; any AdapterError is treated as ack-not-ok."""
        try:
            return await adapter.amend(cid, new_price=new_price, new_qty=new_qty)
        except AdapterError as exc:
            return AmendAck(ok=False, semantics="native", reason=str(exc))

    async def _restore_partial(self, cid: str, filled_qty: int) -> None:
        """Two-step restore: NEW -> PARTIALLY_FILLED when the order is partly filled."""
        if filled_qty > 0:
            await repositories.update_status(self._pool, cid, OrderState.PARTIALLY_FILLED)

    async def mass_cancel(self) -> tuple[list[str], list[str]]:
        """Flatten-and-halt sweep: best-effort cancel of every open order."""
        cancelled: list[str] = []
        failed: list[str] = []
        for row in await repositories.fetch_open_orders(self._pool):
            try:
                adapter = self._resolve_adapter(row.broker)
                await repositories.update_status(
                    self._pool, row.client_order_id, OrderState.PENDING_CANCEL
                )
                ack = await adapter.cancel(row.client_order_id)
                if not ack.ok:
                    raise AdapterError(f"cancel not confirmed: {ack.reason}")
                await repositories.update_status(
                    self._pool, row.client_order_id, OrderState.CANCELLED
                )
                cancelled.append(row.client_order_id)
            except Exception:  # noqa: BLE001 - the sweep must never stop early
                logger.exception("mass-cancel failed for %s", row.client_order_id)
                failed.append(row.client_order_id)
        return cancelled, failed

    # --------------------------------------------------------------------- get
    async def get(self, client_order_id: str) -> NormalizedOrderResult:
        """Aggregate read: order row + fill sums -> NormalizedOrderResult."""
        row = await repositories.fetch_order_result(self._pool, client_order_id)
        if row is None:
            raise OrderNotFound("unknown client_order_id", client_order_id=client_order_id)
        return self._to_result(row)

    def _to_result(self, row: OrderResultRow) -> NormalizedOrderResult:
        return NormalizedOrderResult(
            client_order_id=row.client_order_id,
            broker_order_id=row.broker_order_id,
            broker=row.broker,
            status=to_public_status(row.status, row.filled_qty),
            engine_state=row.status,
            filled_qty=row.filled_qty,
            remaining_qty=row.quantity - row.filled_qty,
            avg_fill_price=row.avg_fill_price,
            reject_reason=row.reject_reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

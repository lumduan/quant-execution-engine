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

from src.quant_execution_engine.adapters.base import BrokerAdapter
from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.adapters.sim import SimAdapter
from src.quant_execution_engine.cache.single_flight import single_flight
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts import capabilities
from src.quant_execution_engine.contracts.enums import OrderState, to_public_status
from src.quant_execution_engine.contracts.errors import (
    ConcurrentSubmit,
    IllegalTransition,
    OrderNotFound,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder, NormalizedOrderResult
from src.quant_execution_engine.core import state_machine
from src.quant_execution_engine.core.kill_switch import KillSwitch
from src.quant_execution_engine.core.risk import RiskGate
from src.quant_execution_engine.core.stage import resolve_adapter
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.errors import DuplicateOrderSignal
from src.quant_execution_engine.db.models import OrderResultRow

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class SubmitOutcome:
    """The result plus whether it was served from the dedupe path."""

    result: NormalizedOrderResult
    duplicate: bool


class OrderRouter:
    """Per-request orchestrator (cheap to build; backed by process singletons)."""

    def __init__(
        self,
        *,
        settings: Settings,
        pool: asyncpg.Pool,
        redis: Any | None,
        liberator_adapter: BrokerAdapter | None = None,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._redis = redis
        self._sim = SimAdapter(default_fill_price=settings.sim_default_fill_price)
        # Injected process singleton (api/deps.py / runtime); None = not configured.
        self._liberator = liberator_adapter
        self._risk = RiskGate(settings, redis)
        self.kill_switch = KillSwitch(settings, redis)

    # ------------------------------------------------------------------ submit
    async def submit(self, order: NormalizedOrder) -> SubmitOutcome:
        """The full frozen submit pipeline."""
        await self.kill_switch.assert_disengaged()  # FIRST (hard rule 3)

        existing = await repositories.fetch_order_result(self._pool, order.client_order_id)
        if existing is not None:
            return SubmitOutcome(result=self._to_result(existing), duplicate=True)

        caps = capabilities.lookup(order.broker, order.market)
        caps.assert_supports(order.order_type, order.tif, order.position_effect)

        await self._risk.check(order)

        adapter = resolve_adapter(
            self._settings.stage,
            order.broker,
            sim_adapter=self._sim,
            liberator_adapter=self._liberator,
        )
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
                await repositories.insert_order(self._pool, order)
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
        adapter = resolve_adapter(
            self._settings.stage,
            row.broker,
            sim_adapter=self._sim,
            liberator_adapter=self._liberator,
        )
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
        new_client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> SubmitOutcome:
        """Cancel+replace orchestration (Liberator has no amend route, R2).

        Not HTTP-exposed in Phase 3 (the route lands Phase 4). The old order
        ends CANCELLED via the frozen PENDING_CANCEL path (PENDING_REPLACE is
        reserved for native amends); the replacement enters the FULL frozen
        submit pipeline under the caller-supplied fresh ``client_order_id``.
        Declared consequences of ``cancel_replace`` semantics: queue priority
        is lost and a brief no-resting-order window exists. Under an engaged
        kill-switch the cancel leg still runs (cancels reduce risk) and the
        replacement is rejected — the position ends flat, never doubled. The
        replacement gets NO PTRM exemption: a price-only amend re-using the
        same quantity inside the duplicate-burst window is risk-rejected
        (old order already cancelled — flat, retry after the window).
        """
        if new_price is None and new_qty is None:
            raise AdapterError("amend requires new_price and/or new_qty")
        row = await repositories.fetch_order(self._pool, client_order_id)
        if row is None:
            raise OrderNotFound("unknown client_order_id", client_order_id=client_order_id)
        await self.cancel(client_order_id)
        replacement = NormalizedOrder(
            client_order_id=new_client_order_id,
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
        return await self.submit(replacement)

    async def mass_cancel(self) -> tuple[list[str], list[str]]:
        """Flatten-and-halt sweep: best-effort cancel of every open order."""
        cancelled: list[str] = []
        failed: list[str] = []
        for row in await repositories.fetch_open_orders(self._pool):
            try:
                adapter = resolve_adapter(
                    self._settings.stage,
                    row.broker,
                    sim_adapter=self._sim,
                    liberator_adapter=self._liberator,
                )
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

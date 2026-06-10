"""``SimAdapter`` — deterministic paper fills, no real money, no broker session.

A pure function of the order: same input ⇒ identical ack (the injected clock
only stamps ``exec_ts``). ``metadata`` is the sim control channel (it is never
sent to a venue by ANY adapter, which is exactly why sim may consume it):

* absent ``sim_fills``      → one full fill → PENDING_NEW → NEW → FILLED
* ``sim_fills: [q1, q2…]``  → partial fills in order (sum < qty rests
  PARTIALLY_FILLED; sum == qty ends FILLED; invalid ⇒ rejected ack)
* ``sim_fills: []``         → rests at NEW (the cancellable fixture)
* ``sim_reject: "reason"``  → rejected ack → PENDING_NEW → REJECTED

TIF: FOK demands exactly one full fill; IOC flags the unfilled remainder as
cancelled (the router walks PENDING_CANCEL → CANCELLED).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    FillReport,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.contracts.capabilities import (
    CAPABILITY_MATRIX,
    CapabilitySet,
)
from src.quant_execution_engine.contracts.enums import Broker, OrderType, Tif
from src.quant_execution_engine.contracts.orders import NormalizedOrder


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SimAdapter(BrokerAdapter):
    """Deterministic in-process venue (the default ``sim``/``paper`` route)."""

    broker: ClassVar[Broker] = Broker.SIM

    def __init__(
        self,
        *,
        default_fill_price: Decimal,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__()
        self._default_fill_price = default_fill_price
        self._now = now

    def _reference_price(self, order: NormalizedOrder) -> Decimal:
        """LIMIT/STOP_LIMIT at price; STOP at stop_price; else the sim default."""
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            assert order.price is not None  # guaranteed by the contract validator
            return order.price
        if order.order_type is OrderType.STOP:
            assert order.stop_price is not None
            return order.stop_price
        if order.price is not None:
            return order.price
        if order.stop_price is not None:
            return order.stop_price
        return self._default_fill_price

    def _fill_plan(self, order: NormalizedOrder) -> list[int] | str:
        """Return the fill quantities, or a rejection reason string."""
        reject = order.metadata.get("sim_reject")
        if reject is not None:
            return str(reject)
        raw = order.metadata.get("sim_fills")
        if raw is None:
            plan = [order.quantity]
        else:
            if not isinstance(raw, list) or not all(
                isinstance(q, int) and not isinstance(q, bool) and q > 0 for q in raw
            ):
                return "invalid sim_fills: must be a list of positive integers"
            plan = list(raw)
            if sum(plan) > order.quantity:
                return "invalid sim_fills: sum exceeds order quantity"
        if order.tif is Tif.FOK and plan != [order.quantity]:
            return "FOK requires a single full fill"
        return plan

    async def place(self, order: NormalizedOrder) -> PlaceAck:
        plan = self._fill_plan(order)
        if isinstance(plan, str):
            return PlaceAck(rejected=True, reject_reason=plan)
        prefix = order.client_order_id[:8]
        price = self._reference_price(order)
        exec_ts = self._now()
        fills = tuple(
            FillReport(
                broker_fill_id=f"SIMF-{prefix}-{i}",
                price=price,
                quantity=qty,
                exec_ts=exec_ts,
            )
            for i, qty in enumerate(plan, start=1)
        )
        remainder_cancelled = order.tif is Tif.IOC and sum(plan) < order.quantity
        return PlaceAck(
            broker_order_id=f"SIM-{prefix}",
            fills=fills,
            remainder_cancelled=remainder_cancelled,
        )

    async def cancel(self, client_order_id: str) -> CancelAck:
        """Sim always confirms a cancel."""
        return CancelAck(ok=True)

    async def amend(
        self,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        """Sim amends natively (declared); no HTTP route until Phase 4."""
        return AmendAck(ok=True, semantics="native")

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        """Sim holds no venue book — the durable store is the truth."""
        return []

    async def get_positions(self, account: str) -> list[Position]:
        return []

    async def get_account(self, account: str) -> AccountInfo:
        return AccountInfo(account=account, buying_power=Decimal("1000000000"))

    def capabilities(self) -> tuple[CapabilitySet, ...]:
        return tuple(entry for entry in CAPABILITY_MATRIX if entry.broker is Broker.SIM)

    async def heartbeat(self) -> bool:
        """Sim sessions are always healthy (HeartbeatHook)."""
        return True

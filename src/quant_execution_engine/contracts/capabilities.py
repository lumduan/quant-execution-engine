"""Capability matrix (shape frozen §F; cells canonical in .claude/knowledge/).

The router enforces this BEFORE any venue I/O (D7): an unsupported
``(broker, market, order_type, tif, position_effect)`` is rejected with a
typed error. Liberator/Settrade rows are declared now (``adapter_installed:
false``) so real-broker-impossible orders reject up front even in sim stage;
every Settrade ``(confirm P4)`` cell is omitted — declare, don't pretend.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderType,
    PositionEffect,
    Tif,
)
from src.quant_execution_engine.contracts.errors import CapabilityError

_ALL_TYPES: tuple[OrderType, ...] = tuple(OrderType)
_ALL_TIFS: tuple[Tif, ...] = tuple(Tif)


class CapabilitySet(BaseModel):
    """One ``(broker, market)`` capability row."""

    model_config = ConfigDict(frozen=True)

    broker: Broker
    market: Market
    order_types: tuple[OrderType, ...]
    tifs: tuple[Tif, ...]
    position_effects: tuple[PositionEffect, ...]
    amend: str  # "native" | "cancel_replace" — declared per adapter, never assumed
    adapter_installed: bool

    def assert_supports(
        self,
        order_type: OrderType,
        tif: Tif,
        position_effect: PositionEffect | None,
    ) -> None:
        """Raise :class:`CapabilityError` for any unsupported combination."""
        if order_type not in self.order_types:
            raise CapabilityError(
                f"order_type {order_type} unsupported for ({self.broker}, {self.market})"
            )
        if tif not in self.tifs:
            raise CapabilityError(f"tif {tif} unsupported for ({self.broker}, {self.market})")
        if position_effect is not None and position_effect not in self.position_effects:
            raise CapabilityError(
                f"position_effect {position_effect} unsupported for ({self.broker}, {self.market})"
            )


CAPABILITY_MATRIX: tuple[CapabilitySet, ...] = (
    CapabilitySet(
        broker=Broker.SIM,
        market=Market.SET,
        order_types=_ALL_TYPES,
        tifs=_ALL_TIFS,
        position_effects=(),
        amend="native",
        adapter_installed=True,
    ),
    CapabilitySet(
        broker=Broker.SIM,
        market=Market.TFEX,
        order_types=_ALL_TYPES,
        tifs=_ALL_TIFS,
        position_effects=(PositionEffect.OPEN, PositionEffect.CLOSE),
        amend="native",
        adapter_installed=True,
    ),
    # Liberator: SET has no stop types; TFEX has no ATO/ATC/MTL; no amend route
    # -> cancel+replace (non-atomic, declared). Cells per capability-matrix.md.
    CapabilitySet(
        broker=Broker.LIBERATOR,
        market=Market.SET,
        order_types=(
            OrderType.MARKET,
            OrderType.LIMIT,
            OrderType.ICEBERG,
            OrderType.MTL,
            OrderType.ATO,
            OrderType.ATC,
        ),
        tifs=_ALL_TIFS,
        position_effects=(),
        amend="cancel_replace",
        adapter_installed=False,
    ),
    CapabilitySet(
        broker=Broker.LIBERATOR,
        market=Market.TFEX,
        order_types=(
            OrderType.MARKET,
            OrderType.LIMIT,
            OrderType.STOP,
            OrderType.STOP_LIMIT,
            OrderType.ICEBERG,
        ),
        tifs=_ALL_TIFS,
        position_effects=(PositionEffect.OPEN, PositionEffect.CLOSE),
        amend="cancel_replace",
        adapter_installed=False,
    ),
    # Settrade (derivatives only): every "(confirm P4)" cell omitted — only
    # LIMIT/STOP/STOP_LIMIT/ICEBERG and DAY are declared until Phase 4 confirms.
    CapabilitySet(
        broker=Broker.SETTRADE,
        market=Market.TFEX,
        order_types=(
            OrderType.LIMIT,
            OrderType.STOP,
            OrderType.STOP_LIMIT,
            OrderType.ICEBERG,
        ),
        tifs=(Tif.DAY,),
        position_effects=(PositionEffect.OPEN, PositionEffect.CLOSE),
        amend="native",
        adapter_installed=False,
    ),
)


def lookup(broker: Broker, market: Market) -> CapabilitySet:
    """Return the capability row for ``(broker, market)`` or raise typed."""
    for entry in CAPABILITY_MATRIX:
        if entry.broker is broker and entry.market is market:
            return entry
    raise CapabilityError(f"market {market} unsupported for broker {broker}")

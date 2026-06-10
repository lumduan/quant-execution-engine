"""Frozen wire contracts (Phase 0 ADR §C–§F).

The lowest layer: a single source of truth for the API surface, the order
router, and every adapter. Nothing in this package performs I/O.
"""

from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderState,
    OrderType,
    PositionEffect,
    PublicOrderStatus,
    Side,
    Stage,
    Tif,
    to_public_status,
)
from src.quant_execution_engine.contracts.orders import (
    NormalizedOrder,
    NormalizedOrderResult,
)

__all__ = [
    "Broker",
    "Market",
    "NormalizedOrder",
    "NormalizedOrderResult",
    "OrderState",
    "OrderType",
    "PositionEffect",
    "PublicOrderStatus",
    "Side",
    "Stage",
    "Tif",
    "to_public_status",
]

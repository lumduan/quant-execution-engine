"""``NormalizedOrder`` / ``NormalizedOrderResult`` — the frozen order language.

Frozen in Phase 0 (ADR §C); cross-field rules mirror the Phase-1 DB CHECKs and
are deliberately boundary-stricter where the contract demands it
(``position_effect`` required for TFEX, ``display_qty`` iff ICEBERG).
``Decimal``-as-string on the wire; ``int`` quantities; UTC timestamps; no
``float`` at any money boundary. ``engine_state`` is the Phase-2 contract
addendum: the public ``status`` enum stays frozen at 6 values while the
internal 9-state truth (the ADR §B reconciliation window included) remains
visible to operators.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderState,
    OrderType,
    PositionEffect,
    PublicOrderStatus,
    Side,
    Tif,
    WireDecimal,
)

_PRICED_TYPES = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT})
_STOP_TYPES = frozenset({OrderType.STOP, OrderType.STOP_LIMIT})


class NormalizedOrder(BaseModel):
    """The single order language every strategy speaks (frozen §C)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    broker: Broker
    account: str = Field(min_length=1)
    market: Market
    symbol: str = Field(min_length=1)
    side: Side
    order_type: OrderType
    price: Decimal | None = None
    stop_price: Decimal | None = None
    quantity: int = Field(gt=0)
    display_qty: int | None = Field(default=None, gt=0)
    tif: Tif
    position_effect: PositionEffect | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("price", "stop_price", mode="before")
    @classmethod
    def _no_float_money(cls, value: object) -> object:
        """Money is Decimal-as-string on the wire — reject binary floats outright."""
        if isinstance(value, float):
            raise ValueError("price fields must be sent as strings, never floats")
        return value

    @field_validator("client_order_id")
    @classmethod
    def _uuid4_at_the_boundary(cls, value: str) -> str:
        """ADR §A: UUIDv4, format-validated here, opaque everywhere after."""
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("client_order_id must be a UUIDv4") from exc
        if parsed.version != 4:
            raise ValueError("client_order_id must be a UUIDv4")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> NormalizedOrder:
        if self.price is not None and self.price <= 0:
            raise ValueError("price must be > 0")
        if self.stop_price is not None and self.stop_price <= 0:
            raise ValueError("stop_price must be > 0")
        if self.order_type in _PRICED_TYPES and self.price is None:
            raise ValueError(f"price is required for {self.order_type}")
        if self.order_type in _STOP_TYPES and self.stop_price is None:
            raise ValueError(f"stop_price is required for {self.order_type}")
        if self.display_qty is not None:
            if self.order_type is not OrderType.ICEBERG:
                raise ValueError("display_qty is only valid for ICEBERG orders")
            if self.display_qty > self.quantity:
                raise ValueError("display_qty must be <= quantity")
        elif self.order_type is OrderType.ICEBERG:
            raise ValueError("ICEBERG orders require display_qty")
        if self.market is Market.TFEX and self.position_effect is None:
            raise ValueError("position_effect is required for TFEX orders")
        if self.market is Market.SET and self.position_effect is not None:
            raise ValueError("position_effect must be omitted for SET orders")
        return self


class NormalizedOrderResult(BaseModel):
    """The normalized ack/state every consumer reads (frozen §C + addendum)."""

    client_order_id: str
    broker_order_id: str | None
    broker: Broker
    status: PublicOrderStatus
    engine_state: OrderState
    filled_qty: int
    remaining_qty: int
    avg_fill_price: WireDecimal | None
    reject_reason: str | None
    created_at: datetime
    updated_at: datetime
    raw: dict[str, Any] | None = None  # private-only; never crosses the public boundary

    def wire_dump(self) -> dict[str, Any]:
        """Public JSON shape: Decimal-as-string, ``raw`` excluded."""
        return self.model_dump(mode="json", exclude={"raw"})

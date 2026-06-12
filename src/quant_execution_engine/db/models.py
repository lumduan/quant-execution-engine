"""Typed row models for the ``execution`` schema (Pydantic at the boundary)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderState,
    OrderType,
    PositionEffect,
    Side,
    Tif,
)


class OrderRow(BaseModel):
    """One ``execution.orders`` row."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    broker: Broker
    broker_order_id: str | None
    account: str
    symbol: str
    market: Market
    side: Side
    order_type: OrderType
    price: Decimal | None
    stop_price: Decimal | None
    quantity: int
    display_qty: int | None
    tif: Tif
    position_effect: PositionEffect | None
    status: OrderState
    reject_reason: str | None
    # Phase 5 (D16): the X-Strategy-Id header, persisted (own infra-db migration).
    # Nullable — pre-Phase-5 orders and header-less submits carry None.
    strategy_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> Self:
        """Build from an asyncpg ``Record`` (or any mapping)."""
        return cls(**{name: record[name] for name in cls.model_fields})


class OrderResultRow(OrderRow):
    """Order row joined with its fill aggregate (one query, GROUP BY PK)."""

    filled_qty: int
    fill_notional: Decimal | None

    @property
    def avg_fill_price(self) -> Decimal | None:
        if self.filled_qty <= 0 or self.fill_notional is None:
            return None
        return self.fill_notional / Decimal(self.filled_qty)


class FillRow(BaseModel):
    """One ``execution.fills`` row."""

    model_config = ConfigDict(frozen=True)

    fill_id: int
    client_order_id: str
    broker_fill_id: str | None
    price: Decimal
    quantity: int
    exec_ts: datetime
    created_at: datetime

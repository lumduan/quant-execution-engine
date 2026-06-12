"""Normalized order-book models (Phase 5, ADR D22).

Frozen Pydantic — not dataclasses — because these cross the API boundary
serialized (umbrella hard rule 3). ``Decimal`` prices, ``int`` volumes, tz-aware
UTC ``received_at``, ``Decimal``-as-string on the wire (matches the order
contract). Identical regardless of source: a consumer cannot tell a Settrade
book from a Liberator book.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.quant_execution_engine.contracts.enums import Market, WireDecimal


class OrderBookSource(StrEnum):
    """Which provider produced a book (carried on the wire as ``source``)."""

    SETTRADE = "settrade"
    LIBERATOR = "liberator"


class OrderBookLevel(BaseModel):
    """One price level: a ``Decimal`` price and an ``int`` resting volume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price: WireDecimal
    volume: int = Field(ge=0)


class OrderBook(BaseModel):
    """A normalized L2 snapshot (depth ≤ 10; index 0 = best)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    market: Market
    bid_levels: list[OrderBookLevel]
    ask_levels: list[OrderBookLevel]
    bid_flag: str = "NORMAL"
    ask_flag: str = "NORMAL"
    sequence: int
    source: OrderBookSource
    received_at: datetime

    @property
    def best_bid(self) -> OrderBookLevel | None:
        """The top bid level, or ``None`` for an empty bid side."""
        return self.bid_levels[0] if self.bid_levels else None

    @property
    def best_ask(self) -> OrderBookLevel | None:
        """The top ask level, or ``None`` for an empty ask side."""
        return self.ask_levels[0] if self.ask_levels else None

    def wire_dump(self) -> dict[str, Any]:
        """Public JSON shape: ``Decimal``-as-string, ``datetime`` ISO-UTC."""
        return self.model_dump(mode="json")

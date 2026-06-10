"""The frozen 7-method ``BrokerAdapter`` interface (Phase 0 ADR §D).

Every adapter (``SimAdapter``, ``LiberatorAdapter``, ``SettradeAdapter``)
implements exactly: place / cancel / amend / get_open_orders / get_positions /
get_account / capabilities. Amend semantics are DECLARED per adapter, never
assumed — callers query ``capabilities()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from src.quant_execution_engine.adapters.session import SessionCircuitBreaker
from src.quant_execution_engine.contracts.capabilities import CapabilitySet
from src.quant_execution_engine.contracts.enums import Broker, Market
from src.quant_execution_engine.contracts.orders import NormalizedOrder


class FillReport(BaseModel):
    """One execution reported by a venue (or synthesised by sim)."""

    model_config = ConfigDict(frozen=True)

    broker_fill_id: str
    price: Decimal
    quantity: int = Field(gt=0)
    exec_ts: datetime


class PlaceAck(BaseModel):
    """Adapter response to ``place`` — venue ack or rejection."""

    model_config = ConfigDict(frozen=True)

    rejected: bool = False
    reject_reason: str | None = None
    broker_order_id: str | None = None
    fills: tuple[FillReport, ...] = ()
    remainder_cancelled: bool = False  # IOC semantics: unfilled remainder cancelled


class CancelAck(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool = True
    reason: str | None = None


class AmendAck(BaseModel):
    """Amend semantics are declared, never assumed (R2)."""

    model_config = ConfigDict(frozen=True)

    ok: bool = True
    semantics: str = "native"  # "native" | "cancel_replace"
    reason: str | None = None


class Position(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: str
    market: Market
    symbol: str
    net_qty: int


class AccountInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: str
    buying_power: Decimal


class BrokerAdapter(ABC):
    """Frozen interface — exactly these seven methods (§D)."""

    broker: ClassVar[Broker]

    def __init__(self) -> None:
        self.breaker = SessionCircuitBreaker()

    @abstractmethod
    async def place(self, order: NormalizedOrder) -> PlaceAck:
        """Route one normalized order to the venue (or sim)."""

    @abstractmethod
    async def cancel(self, client_order_id: str) -> CancelAck:
        """Cancel the venue order mapped to ``client_order_id``."""

    @abstractmethod
    async def amend(
        self,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        """Amend price/qty; semantics per ``capabilities()`` (native vs cancel+replace)."""

    @abstractmethod
    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        """Venue-truth open orders for reconciliation (ADR §B)."""

    @abstractmethod
    async def get_positions(self, account: str) -> list[Position]:
        """Normalized positions."""

    @abstractmethod
    async def get_account(self, account: str) -> AccountInfo:
        """Normalized account / buying power."""

    @abstractmethod
    def capabilities(self) -> tuple[CapabilitySet, ...]:
        """Per-``(broker, market)`` capability sets this adapter declares."""

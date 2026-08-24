"""The frozen 7-method ``BrokerAdapter`` interface (Phase 0 ADR §D).

Every adapter (``SimAdapter``, ``LiberatorAdapter``, ``StreamingProAdapter``)
implements exactly: place / cancel / amend / get_open_orders / get_positions /
get_account / capabilities. Amend semantics are DECLARED per adapter, never
assumed — callers query ``capabilities()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.quant_execution_engine.adapters.session import SessionCircuitBreaker
from src.quant_execution_engine.contracts.capabilities import CapabilitySet
from src.quant_execution_engine.contracts.enums import Broker, Market, WireDecimal
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


class AccountType(StrEnum):
    """Which balance sheet an account keeps — the discriminator for the margin block.

    Named from the venue's own ``type`` string (Liberator ``/va/profile`` returns
    ``"CASH BALANCE"`` / ``"DERIVATIVE"``). ``UNKNOWN`` is for brokers that do not say.
    """

    CASH = "cash"
    DERIVATIVE = "derivative"
    UNKNOWN = "unknown"


class AccountInfo(BaseModel):
    """A normalized account snapshot.

    🔴 **Every optional field means "this broker did not report it", NEVER "it is zero".**
    That distinction is the whole of [[TK-0396]]: a ``0`` returned for something the venue
    never sent is indistinguishable from a real zero, and the adapter that did exactly that
    reported ``buying_power=0`` for accounts holding real five-figure balances.

    Coverage is **deliberately asymmetric**, because the venues are
    (``docs/reference/liberator-account-reads.md``):

    ==========================  ==========  ================  ==============
    field                       Liberator   Liberator DERIV   Streaming Pro
    ==========================  ==========  ================  ==============
    ``buying_power``            ✔           ✔                 ✔ (SET only)
    ``cash_balance``            ✔           ✔                 ✔ (SET only)
    ``credit_limit``            ✔           ✔                 ✔ (SET only)
    ``withdrawable``            ✔           ✔                 ✘
    ``equity`` ``excess_equity``  ✘         ✔                 ✘
    ``initial_margin`` ``maintenance_margin``  ✘  ✔           ✘
    ==========================  ==========  ================  ==============

    ⇒ the margin block is fillable by **one of six (broker, market) cells**. Modelling it as
    required would have forced five of them to lie.
    """

    model_config = ConfigDict(frozen=True)

    account: str
    account_type: AccountType = AccountType.UNKNOWN

    # Buying power. Kept as the one REQUIRED money field so every existing caller is unaffected.
    buying_power: WireDecimal

    # Cash / credit — the only tier both brokers supply.
    cash_balance: WireDecimal | None = None
    credit_limit: WireDecimal | None = None
    withdrawable: WireDecimal | None = None

    # Margin — DERIVATIVE accounts only. See the validator: forbidden elsewhere, not merely absent.
    equity: WireDecimal | None = None
    excess_equity: WireDecimal | None = None
    initial_margin: WireDecimal | None = None
    maintenance_margin: WireDecimal | None = None

    @model_validator(mode="after")
    def _margin_block_belongs_to_derivatives(self) -> AccountInfo:
        """Forbid the margin block on a non-derivative account — both directions.

        Copying ``NormalizedOrder``'s TFEX/``position_effect`` rule, which enforces
        required-when *and* forbidden-when. Enforcing only "optional" would let a cash
        account carry a margin figure that cannot exist, and nothing would object.

        Not required-when: a DERIVATIVE account read from a broker that reports no margin
        is legitimately all-``None`` — that is the asymmetry, not an error.
        """
        if self.account_type is AccountType.DERIVATIVE:
            return self
        named = [
            n
            for n in ("equity", "excess_equity", "initial_margin", "maintenance_margin")
            if getattr(self, n) is not None
        ]
        if named:
            raise ValueError(
                f"{', '.join(named)} is only valid on a DERIVATIVE account "
                f"(account_type={self.account_type.value})"
            )
        return self


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

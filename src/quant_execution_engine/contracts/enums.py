"""Frozen enums (Phase 0 ADR §C/§E) + wire-serialization helpers.

``OrderState`` is the INTERNAL 9-value set — exactly the DB CHECK set enforced
by ``execution.orders.status`` (Phase 1). ``PublicOrderStatus`` is the frozen
6-value Result enum; local pending states map onto it via
:func:`to_public_status` and the truthful internal value travels in the
additive ``engine_state`` Result field (Phase 2 contract addendum).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import PlainSerializer


class Broker(StrEnum):
    SIM = "sim"
    LIBERATOR = "liberator"
    STREAMING_PRO = "streaming_pro"


class Market(StrEnum):
    SET = "SET"
    TFEX = "TFEX"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    ICEBERG = "ICEBERG"
    MTL = "MTL"
    ATO = "ATO"
    ATC = "ATC"


class Tif(StrEnum):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"
    GTC = "GTC"


class PositionEffect(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class Stage(StrEnum):
    """The safety ladder (E2). ``sim`` is the default; ``live`` is gated."""

    SIM = "sim"
    PAPER = "paper"
    MICRO_LIVE = "micro_live"
    LIVE = "live"


class OrderState(StrEnum):
    """Internal 9-state machine (frozen §E; DB CHECK set)."""

    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PublicOrderStatus(StrEnum):
    """Frozen public Result status enum (§C) — never carries local states."""

    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


# Decimal-as-string on the wire (never float at a money boundary).
# format(d, "f") avoids scientific notation for any magnitude.
WireDecimal = Annotated[
    Decimal,
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
]


def to_public_status(state: OrderState, filled_qty: int) -> PublicOrderStatus:
    """Map the internal state onto the frozen public enum.

    ``PENDING_NEW`` surfaces as ``NEW`` (submitted, not terminal);
    ``PENDING_CANCEL``/``PENDING_REPLACE`` surface as the closest fill-aware
    resting status. The unmapped truth travels in ``engine_state``.
    """
    if state is OrderState.PENDING_NEW:
        return PublicOrderStatus.NEW
    if state in (OrderState.PENDING_CANCEL, OrderState.PENDING_REPLACE):
        return PublicOrderStatus.PARTIALLY_FILLED if filled_qty > 0 else PublicOrderStatus.NEW
    return PublicOrderStatus(state.value)


class HandleResolution(StrEnum):
    """How much the engine actually KNOWS about a just-submitted order (TK-0423).

    Emitted on the ``POST /orders`` response. It answers a question the order
    state cannot: *did we read the venue, or are we guessing?*

    🔴 ``PENDING`` and ``UNKNOWN`` must never be collapsed. They differ in the
    only way that costs money: under ``PENDING`` the venue was read and the
    order is working; under ``UNKNOWN`` the venue could **not** be read, so the
    order may be live with its handle unrecovered — and a retry double-fills.
    That is the discriminator problem this project keeps paying for: a signal
    whose healthy reading is produced by the same path as its failure.
    """

    CONFIRMED = "confirmed"
    """The venue was read and answered: state and handle are venue truth."""

    PENDING = "pending"
    """The venue was read; the order is accepted but not yet resolvable there.
    Safe to wait on — it is working. NEVER resubmit."""

    UNKNOWN = "unknown"
    """The venue could NOT be read within the deadline. The order may be LIVE.
    🔴 NEVER resubmit; reconcile or read ``GET /orders/{cid}`` instead."""

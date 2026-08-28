"""Pure mapping between the frozen normalized contract and the Streaming-Pro bridge wire.

The bridge accepts the engine's UPPERCASE enums as input (it maps them to the broker's capitalized
form itself), so the write side is mostly pass-through — the capability gate (D7) has already
rejected anything outside ``(MARKET, LIMIT) × DAY`` before these run. Prices cross as
Decimal-as-string (``format(d, "f")``) — never a float in engine code; the bridge's pydantic float
field coerces the string. The engine stamps NO PIN (the bridge owns it).
"""

from __future__ import annotations

import hashlib
import uuid
from enum import StrEnum
from typing import Any
from urllib.parse import urlencode

from src.quant_execution_engine.adapters.streaming_pro.errors import StreamingProMappingError
from src.quant_execution_engine.adapters.streaming_pro.models import VenueOrderRow
from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderType,
    PositionEffect,
    Side,
    Tif,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder

# ---------------------------------------------------------------- paths


def place_path(market: Market) -> str:
    return "order/place/set" if market is Market.SET else "order/place/tfex"


def cancel_path() -> str:
    return "order/cancel"


def orders_path(account: str, market: Market) -> str:
    return "orders?" + urlencode({"account": account, "market": market.value})


def portfolio_path(account: str) -> str:
    return "portfolio?" + urlencode({"account": account})


def account_path(account: str) -> str:
    """SET/equity balance — the bridge hardcodes the ``fis`` (equity) front."""
    return "account-info?" + urlencode({"account": account})


def tfex_account_path(account: str) -> str:
    """TFEX/derivatives balance — a DIFFERENT venue front, not a parameter of the SET one.

    🔑 The two fronts are **mutually exclusive**, measured 2026-08-27 on the live venue:
    the SET route answers ``FISGW-00 UserAccount not found`` for a TFEX account, and this
    route answers ``GWD-03 UserAccount not found`` for a SET account. **So the VENUE
    decides which market an account belongs to — the adapter never infers it from the
    account number**, which matters because SET ``0500007`` and TFEX ``0500009`` differ by
    one digit and guessing from the pattern is exactly how the wrong market gets queried.
    """
    return "tfex/account-info?" + urlencode({"account": account})


# ---------------------------------------------------------------- write side


def _wire_price(order: NormalizedOrder) -> str | None:
    """Decimal-as-string price, or None for a price-less market order (bridge defaults to 0)."""
    price = order.price if order.price is not None else order.stop_price
    return format(price, "f") if price is not None else None


def to_set_payload(order: NormalizedOrder) -> dict[str, Any]:
    """``NormalizedOrder`` → the bridge's ``SetOrderRequest`` body (no PIN — bridge-stamped)."""
    body: dict[str, Any] = {
        "symbol": order.symbol,
        "side": order.side.value,  # BUY|SELL (bridge maps to Buy|Sell)
        "volume": order.quantity,
        "account": order.account,
        "price_type": order.order_type.value,  # LIMIT|MARKET (bridge maps to Limit|Market)
        "validity": order.tif.value,  # DAY
    }
    price = _wire_price(order)
    if price is not None:
        body["price"] = price
    return body


def to_tfex_payload(order: NormalizedOrder) -> dict[str, Any]:
    """``NormalizedOrder`` → the bridge's ``TfexOrderRequest`` body (no PIN — bridge-stamped)."""
    if order.position_effect is None:
        raise StreamingProMappingError("TFEX orders require position_effect")
    body: dict[str, Any] = {
        "symbol": order.symbol,
        "side": order.side.value,  # BUY|SELL (bridge maps to Long|Short)
        "position": order.position_effect.value,  # OPEN|CLOSE
        "volume": order.quantity,
        "account": order.account,
        "price_type": order.order_type.value,
        "validity": order.tif.value,
    }
    price = _wire_price(order)
    if price is not None:
        body["price"] = price
    return body


def to_place_payload(order: NormalizedOrder) -> dict[str, Any]:
    if order.market is Market.SET:
        return to_set_payload(order)
    return to_tfex_payload(order)


def to_cancel_payload(
    *, order_no: str, market: Market, account: str, symbol: str | None, ext_order_no: str | None
) -> dict[str, Any]:
    """Cancel body. TFEX needs only ``order_no``; SET also needs ``ext_order_no`` + ``symbol``."""
    body: dict[str, Any] = {"order_no": order_no, "market": market.value, "account": account}
    if market is Market.SET:
        body["ext_order_no"] = ext_order_no or ""
        body["symbol"] = symbol or ""
    return body


# ---------------------------------------------------------------- read side

_READ_SIDES: dict[str, Side] = {
    "B": Side.BUY,
    "BUY": Side.BUY,
    "LONG": Side.BUY,
    "S": Side.SELL,
    "SELL": Side.SELL,
    "SHORT": Side.SELL,
}
_READ_PRICE_TYPES: dict[str, OrderType] = {"LIMIT": OrderType.LIMIT, "MARKET": OrderType.MARKET}
_READ_POSITIONS: dict[str, PositionEffect] = {
    "OPEN": PositionEffect.OPEN,
    "CLOSE": PositionEffect.CLOSE,
}


def from_venue_side(raw: str) -> Side | None:
    return _READ_SIDES.get(raw.strip().upper())


def _synthetic_client_order_id(order_no: str) -> str:
    """Deterministic UUIDv4-shaped placeholder for an uncorrelated venue row (read-only view)."""
    digest = bytearray(hashlib.sha256(f"streaming_pro:{order_no}".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def venue_row_to_normalized(
    row: VenueOrderRow, *, account: str, market: Market
) -> NormalizedOrder | None:
    """Best-effort venue row → ``NormalizedOrder`` (read-only view, ADR §B); unknowns → None."""
    side = from_venue_side(row.side)
    order_type = _READ_PRICE_TYPES.get(row.price_type.strip().upper())
    if side is None or order_type is None:
        return None
    position = _READ_POSITIONS.get((row.position or "").strip().upper())
    try:
        return NormalizedOrder(
            client_order_id=_synthetic_client_order_id(row.order_no),
            broker=Broker.STREAMING_PRO,
            account=row.account_no or account,
            market=market,
            symbol=row.symbol,
            side=side,
            order_type=order_type,
            price=row.price,
            quantity=row.volume,
            tif=Tif.DAY,
            position_effect=position if market is Market.TFEX else None,
        )
    except ValueError:
        return None


class VenueOrderState(StrEnum):
    """Coarse venue classification driving the reconciler (fills derived separately)."""

    RESTING = "resting"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


_CANCELLED_STATUS = frozenset({"CANCELLED", "CANCELED", "CXL"})
_REJECTED_STATUS = frozenset({"REJECTED", "REJECT"})
_EXPIRED_STATUS = frozenset({"EXPIRED", "EXPIRE"})


def classify_venue_state(row: VenueOrderRow) -> VenueOrderState:
    """Map venue status/rejectReason/counters onto the coarse classification (conservative).

    A non-empty ``rejectReason`` wins; explicit status words match next; a fully-cancelled counter
    triple is the numeric fallback; anything unrecognized is RESTING (the reconciler then acts only
    on the cumulative ``matched`` counter — never guesses a terminal state).
    """
    if row.reject_reason.strip():
        return VenueOrderState.REJECTED
    blob = f"{row.status} {row.status_show}".upper()
    if any(token in blob for token in _CANCELLED_STATUS):
        return VenueOrderState.CANCELLED
    if any(token in blob for token in _REJECTED_STATUS):
        return VenueOrderState.REJECTED
    if any(token in blob for token in _EXPIRED_STATUS):
        return VenueOrderState.EXPIRED
    if row.cancelled > 0 and row.balance == 0 and row.matched < row.volume:
        return VenueOrderState.CANCELLED
    return VenueOrderState.RESTING

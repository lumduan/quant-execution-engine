"""Pure mapping between the frozen normalized contract and the Settrade wire.

Write side (``NormalizedOrder`` → payload dicts) and read side (venue
``SettradeOrderItem`` → engine-facing classification). Everything here is a pure
function over already-validated inputs — the capability gate (D7) rejects
unsupported combinations BEFORE any of these run, so an unmappable order reaching
this module is a programming error surfaced as :class:`SettradeMappingError`
(which the adapter converts to a rejected ack, never a silent drop).

The two books spell the same concepts differently: SET equity (``Buy``/``Sell``,
no position, ``/api/seos/v3``) and TFEX derivatives (``Long``/``Short``, OPEN/
CLOSE position, stop conditions, ``/api/seosd/v3``). Path builders return relative
paths (no leading slash — the client joins them against ``base_url``).

Enum sets are pinned from the official venue docs + the ``settrade-v2`` 2.2.1 SDK
(see ``/tmp/settrade_docs/PINNED.md``). Prices ride the wire as JSON ``float``
(the venue contract) but only after an exact ``Decimal`` round-trip — a price that
cannot survive ``float()`` losslessly is rejected, never re-quantized. The price-0
sentinels (ATO/ATC/MP-MTL/MP-MKT, the STOP market leg) ship a literal ``0``.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any

from src.quant_execution_engine.adapters.settrade.errors import SettradeMappingError
from src.quant_execution_engine.adapters.settrade.models import SettradeOrderItem
from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderType,
    PositionEffect,
    Side,
    Tif,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder

# Write-side vocabulary (pinned — PINNED.md "Engine capability-cell pinning").
_SET_PRICE_TYPES: dict[OrderType, str] = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "MP-MKT",
    OrderType.MTL: "MP-MTL",  # market-to-limit
    OrderType.ATO: "ATO",
    OrderType.ATC: "ATC",
    OrderType.ICEBERG: "Limit",  # iceberg = Limit + qtyOpen
}
_TFEX_PRICE_TYPES: dict[OrderType, str] = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "MP-MKT",
    OrderType.MTL: "MP-MTL",
    OrderType.ATO: "ATO",
    OrderType.STOP: "MP-MKT",  # stop-market leg + stop trio
    OrderType.STOP_LIMIT: "Limit",  # stop-limit = Limit + price + stop trio
    OrderType.ICEBERG: "Limit",
}
# `Date`(GTD) has no Tif enum member -> deliberately undeclared (v1).
_VALIDITY: dict[Tif, str] = {
    Tif.DAY: "Day",
    Tif.IOC: "IOC",
    Tif.FOK: "FOK",
    Tif.GTC: "Cancel",  # the venue's GTC spelling (max 254 days)
}
_SET_SIDES: dict[Side, str] = {Side.BUY: "Buy", Side.SELL: "Sell"}
_TFEX_SIDES: dict[Side, str] = {Side.BUY: "Long", Side.SELL: "Short"}
# `Auto` is undeclared (extra permission) — OPEN/CLOSE only.
_POSITIONS: dict[PositionEffect, str] = {
    PositionEffect.OPEN: "Open",
    PositionEffect.CLOSE: "Close",
}
# Stop-condition v1 pin (NormalizedOrder has no condition field): derive from
# side. SESSION-trigger stops are undeclared in v1.
_STOP_CONDITIONS: dict[Side, str] = {
    Side.BUY: "LAST_PAID_OR_HIGHER",
    Side.SELL: "LAST_PAID_OR_LOWER",
}

# Order types whose market leg ships a literal price 0 on the wire.
_PRICE_ZERO_TYPES: frozenset[OrderType] = frozenset(
    {OrderType.MARKET, OrderType.MTL, OrderType.ATO, OrderType.ATC, OrderType.STOP}
)
_STOP_TYPES: frozenset[OrderType] = frozenset({OrderType.STOP, OrderType.STOP_LIMIT})


# ------------------------------------------------------------------- paths


def _equity_base(broker_id: str, account: str) -> str:
    return f"api/seos/v3/{broker_id}/accounts/{account}"


def _derivatives_base(broker_id: str, account: str) -> str:
    return f"api/seosd/v3/{broker_id}/accounts/{account}"


def _book_base(broker_id: str, account: str, market: Market) -> str:
    if market is Market.SET:
        return _equity_base(broker_id, account)
    return _derivatives_base(broker_id, account)


def orders_path(broker_id: str, account: str, market: Market) -> str:
    """POST place / GET list of orders."""
    return f"{_book_base(broker_id, account, market)}/orders"


def order_path(broker_id: str, account: str, market: Market, order_no: str) -> str:
    """GET a single order by venue order number."""
    return f"{_book_base(broker_id, account, market)}/orders/{order_no}"


def change_path(broker_id: str, account: str, market: Market, order_no: str) -> str:
    """PATCH native amend (`.../orders/{order_no}/change`)."""
    return f"{_book_base(broker_id, account, market)}/orders/{order_no}/change"


def cancel_path(broker_id: str, account: str, market: Market, order_no: str) -> str:
    """PATCH single cancel (`.../orders/{order_no}/cancel`)."""
    return f"{_book_base(broker_id, account, market)}/orders/{order_no}/cancel"


def bulk_cancel_path(broker_id: str, account: str, market: Market) -> str:
    """PATCH bulk cancel (`.../cancel`)."""
    return f"{_book_base(broker_id, account, market)}/cancel"


def trades_path(broker_id: str, account: str, market: Market) -> str:
    """GET trades — equity trades live on v4, derivatives on v3."""
    if market is Market.SET:
        return f"api/seos/v4/{broker_id}/accounts/{account}/trades"
    return f"{_derivatives_base(broker_id, account)}/trades"


def account_info_path(broker_id: str, account: str, market: Market) -> str:
    """GET account info (`.../account-info`)."""
    return f"{_book_base(broker_id, account, market)}/account-info"


def portfolios_path(broker_id: str, account: str, market: Market) -> str:
    """GET portfolios (`.../portfolios`)."""
    return f"{_book_base(broker_id, account, market)}/portfolios"


def wire_order_no(market: Market, order_no: str) -> int | str:
    """Venue order number on the wire: ``int`` for TFEX, ``str`` for SET.

    Derivatives ``orderNo`` is an integer on the venue wire; a non-numeric TFEX
    order number is a programming error surfaced as :class:`SettradeMappingError`
    (never silently coerced). SET passes the string through unchanged.
    """
    if market is Market.SET:
        return order_no
    try:
        return int(order_no)
    except (TypeError, ValueError) as exc:
        raise SettradeMappingError(
            f"TFEX venue order number must be numeric (got {order_no!r})"
        ) from exc


# ------------------------------------------------------------------- price


def wire_price(price: Decimal) -> float:
    """Convert a ``Decimal`` price to the venue's JSON ``float`` — exactly.

    The Settrade JSON contract carries prices as numbers, so we cross the
    boundary as ``float`` (the one sanctioned place — every other money value
    stays ``Decimal``). A tick-sized price round-trips losslessly; a price that
    ``float()`` cannot represent exactly is REJECTED, never silently re-quantized
    (that would alter the limit). The round-trip check ``Decimal(repr(f)) ==
    price`` is the exactness guard.
    """
    as_float = float(price)
    if Decimal(repr(as_float)) != price:
        raise SettradeMappingError(
            f"price {price} cannot be represented exactly as a wire float; "
            "supply a tick-sized price (never re-quantized)"
        )
    return as_float


def _wire_price_field(order: NormalizedOrder) -> int | float:
    """The ``price`` wire field: literal ``0`` for the price-0 family, else exact.

    ATO/ATC/MP-MTL/MP-MKT (and the STOP market leg) send a literal ``0``; every
    priced type (LIMIT/STOP_LIMIT/ICEBERG) sends the exact wire float. A priced
    type without a price is a contract violation — belt-and-braces like Liberator.
    """
    if order.order_type in _PRICE_ZERO_TYPES:
        return 0
    if order.price is None:  # pragma: no cover - contract guarantees a price here
        raise SettradeMappingError(
            f"order_type {order.order_type} requires a price on the Settrade wire"
        )
    return wire_price(order.price)


# ------------------------------------------------------------------- write side


def _venue_price_type(order_type: OrderType, market: Market) -> str:
    table = _SET_PRICE_TYPES if market is Market.SET else _TFEX_PRICE_TYPES
    mapped = table.get(order_type)
    if mapped is None:
        raise SettradeMappingError(
            f"order_type {order_type} is not expressible on the Settrade {market} wire"
        )
    return mapped


def _venue_validity(tif: Tif) -> str:
    mapped = _VALIDITY.get(tif)
    if mapped is None:  # pragma: no cover - capability gate rejects undeclared tifs
        raise SettradeMappingError(f"tif {tif} is not expressible on the Settrade wire")
    return mapped


def _to_set_payload(order: NormalizedOrder, *, pin: str) -> dict[str, Any]:
    """``NormalizedOrder`` → SET equity place body (``/api/seos/v3/.../orders``)."""
    if order.position_effect is not None:
        raise SettradeMappingError("SET equity orders carry no position_effect")
    price_type = _venue_price_type(order.order_type, Market.SET)
    if order.order_type in (OrderType.LIMIT, OrderType.ICEBERG) and order.price is None:
        raise SettradeMappingError(
            f"order_type {order.order_type} requires a price on the Settrade SET wire"
        )
    return {
        "pin": pin,
        "side": _SET_SIDES[order.side],
        "symbol": order.symbol,
        "trusteeIdType": "Local",  # NVDR out of scope v1 (no contract field)
        "volume": order.quantity,
        "qtyOpen": order.display_qty or 0,  # iceberg display volume (0 = none)
        "price": _wire_price_field(order),
        "priceType": price_type,
        "validityType": _venue_validity(order.tif),
        "clientType": "Individual",
    }


def _to_tfex_payload(order: NormalizedOrder, *, pin: str) -> dict[str, Any]:
    """``NormalizedOrder`` → TFEX derivatives place body (``/api/seosd/v3/.../orders``).

    ``bypassWarning`` / ``triggerSession`` / ``validityDateCondition`` are
    deliberately never sent (undeclared v1). ``None``-valued keys are dropped
    (venue convention). Stop fields ride only for STOP/STOP_LIMIT.
    """
    if order.position_effect is None:
        raise SettradeMappingError("TFEX orders require position_effect")
    payload: dict[str, Any] = {
        "symbol": order.symbol,
        "side": _TFEX_SIDES[order.side],
        "position": _POSITIONS[order.position_effect],
        "priceType": _venue_price_type(order.order_type, Market.TFEX),
        "price": _wire_price_field(order),
        "volume": order.quantity,
        "validityType": _venue_validity(order.tif),
        "pin": pin,
    }
    if order.order_type is OrderType.ICEBERG or order.display_qty:
        payload["icebergVol"] = order.display_qty
    if order.order_type in _STOP_TYPES:
        if order.stop_price is None:
            raise SettradeMappingError(
                f"order_type {order.order_type} requires stop_price on the Settrade TFEX wire"
            )
        payload["stopCondition"] = _STOP_CONDITIONS[order.side]
        payload["stopSymbol"] = order.symbol
        payload["stopPrice"] = wire_price(order.stop_price)
    return {key: value for key, value in payload.items() if value is not None}


def to_place_payload(order: NormalizedOrder, *, pin: str) -> dict[str, Any]:
    """``NormalizedOrder`` → place body, dispatched by market."""
    if order.market is Market.SET:
        return _to_set_payload(order, pin=pin)
    return _to_tfex_payload(order, pin=pin)


def to_change_payload(
    market: Market,
    *,
    pin: str,
    new_price: Decimal | None,
    new_qty: int | None,
) -> dict[str, Any]:
    """Native-amend body (same shape for both books).

    Equity's ``newTrusteeIdType``/``newIcebergVolume`` are deliberately not
    exposed in v1. At least one of ``new_price``/``new_qty`` is required.
    """
    if new_price is None and new_qty is None:
        raise SettradeMappingError("amend requires new_price or new_qty")
    payload: dict[str, Any] = {"pin": pin}
    if new_price is not None:
        payload["newPrice"] = wire_price(new_price)
    if new_qty is not None:
        payload["newVolume"] = new_qty
    return payload


def to_cancel_payload(pin: str) -> dict[str, Any]:
    """Single-cancel body (cancel is by venue order number in the path)."""
    return {"pin": pin}


def to_bulk_cancel_payload(pin: str, order_nos: list[str], market: Market) -> dict[str, Any]:
    """Bulk-cancel body: ``{pin, orders: [wire_order_no...]}``."""
    return {"pin": pin, "orders": [wire_order_no(market, no) for no in order_nos]}


# ------------------------------------------------------------------- read side


def from_venue_side(raw: str) -> Side | None:
    """Read-side side parsing (case-insensitive); unknown → ``None``.

    The venue uses ``Buy``/``Sell`` (equity) and ``Long``/``Short`` (deriv); some
    surfaces abbreviate to ``B``/``S``.
    """
    token = raw.strip().upper()
    if token in ("BUY", "LONG", "B"):
        return Side.BUY
    if token in ("SELL", "SHORT", "S"):
        return Side.SELL
    return None


_READ_PRICE_TYPES: dict[str, OrderType] = {
    "LIMIT": OrderType.LIMIT,
    "MP-MKT": OrderType.MARKET,
    "MP-MTL": OrderType.MTL,
    "ATO": OrderType.ATO,
    "ATC": OrderType.ATC,
}
_READ_VALIDITY: dict[str, Tif] = {
    "DAY": Tif.DAY,
    "IOC": Tif.IOC,
    "FOK": Tif.FOK,
    "CANCEL": Tif.GTC,
}


def _synthetic_client_order_id(order_no: str) -> str:
    """Deterministic UUIDv4-shaped placeholder for an uncorrelated venue row.

    ``get_open_orders`` must return ``NormalizedOrder`` (frozen §D) but the
    venue does not know our client ids. The placeholder is a stable hash of the
    venue order number with the v4 version/variant bits set so it passes the
    contract validator; it is a READ-ONLY view id and is never persisted.
    """
    digest = bytearray(hashlib.sha256(f"settrade:{order_no}".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def venue_item_to_normalized(
    item: SettradeOrderItem, *, account: str, market: Market
) -> NormalizedOrder | None:
    """Best-effort venue row → ``NormalizedOrder`` (read-only view, ADR §B).

    Rows the frozen contract cannot represent — unmappable side, unmappable price
    type, unknown validity, or a TFEX row whose position cannot be classified —
    return ``None`` and are skipped by the caller (a read view never guesses).
    An equity row carries no position; a derivatives row requires OPEN/CLOSE.
    """
    side = from_venue_side(item.side)
    order_type = _READ_PRICE_TYPES.get(item.price_type.strip().upper())
    tif = _READ_VALIDITY.get(item.validity.strip().upper())
    if side is None or order_type is None or tif is None:
        return None
    if order_type is OrderType.LIMIT and item.iceberg_vol > 0:
        order_type = OrderType.ICEBERG
    position: PositionEffect | None = None
    if market is Market.TFEX:
        position = _POSITION_READ.get((item.position or "").strip().upper())
        if position is None:
            return None
    try:
        return NormalizedOrder(
            client_order_id=_synthetic_client_order_id(item.order_no),
            broker=Broker.SETTRADE,
            account=item.account_no or account,
            market=market,
            symbol=item.symbol,
            side=side,
            order_type=order_type,
            price=item.price,
            quantity=item.quantity,
            display_qty=item.iceberg_vol if order_type is OrderType.ICEBERG else None,
            tif=tif,
            position_effect=position,
        )
    except ValueError:
        return None


_POSITION_READ: dict[str, PositionEffect] = {
    "OPEN": PositionEffect.OPEN,
    "CLOSE": PositionEffect.CLOSE,
}


class VenueOrderState(StrEnum):
    """Coarse venue classification driving the reconciler's transitions.

    Fills are NOT a state here — they are derived from the cumulative
    ``matched`` counter independently of this classification.
    """

    RESTING = "resting"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Bare status letter codes (pinned-so-far; the venue does not document the full
# vocabulary): deriv `E`=Expired, equity `CS`=cancel-confirmed. Unknown codes
# fall through to RESTING by design (E20) — the reconciler then only trusts the
# matched counter, never a guessed terminal state.
_STATUS_CODES: dict[str, VenueOrderState] = {
    "E": VenueOrderState.EXPIRED,
    "C": VenueOrderState.CANCELLED,
    "CS": VenueOrderState.CANCELLED,
    "R": VenueOrderState.REJECTED,
    "X": VenueOrderState.CANCELLED,
}


def classify_venue_state(item: SettradeOrderItem) -> VenueOrderState:
    """Map venue reject/status/show-status onto the coarse classification.

    Conservative by design and in this order: a real reject code/reason always
    wins; explicit status WORDS match next (case-insensitive substring on
    ``status``/``show_status``: ``cancel`` → CANCELLED, ``expire`` → EXPIRED,
    ``reject`` → REJECTED); a bare letter code is matched against the
    pinned-so-far table; anything unrecognized is treated as RESTING (the
    reconciler then only acts on the ``matched`` counter — never guesses a
    terminal state). The status vocabularies are not exhaustively documented;
    unknown → RESTING by design (E20).
    """
    if item.rejected:
        return VenueOrderState.REJECTED
    haystack = f"{item.status} {item.show_status}".lower()
    if "cancel" in haystack:
        return VenueOrderState.CANCELLED
    if "expire" in haystack:
        return VenueOrderState.EXPIRED
    if "reject" in haystack:
        return VenueOrderState.REJECTED
    status = item.status.strip().upper()
    matched = _STATUS_CODES.get(status)
    if matched is not None:
        return matched
    return VenueOrderState.RESTING

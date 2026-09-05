"""Pure mapping between the frozen normalized contract and the Liberator wire.

Write side (``NormalizedOrder`` → payload dicts) and read side (venue
``OrderItem`` → engine-facing classification). Everything here is a pure
function over already-validated inputs — the capability gate (D7) rejects
unsupported combinations BEFORE any of these run, so an unmappable order
reaching this module is a programming error surfaced as
:class:`LiberatorMappingError` (which the adapter converts to a rejected ack,
never a silent drop).

Wire payloads contain JSON-safe primitives only: prices are Decimal-as-string
(``format(d, "f")``), quantities are ``int`` — no float ever (umbrella rule).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any

from src.quant_execution_engine.adapters.liberator.errors import LiberatorMappingError
from src.quant_execution_engine.adapters.liberator.models import VenueOrderItem
from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderType,
    PositionEffect,
    Side,
    Tif,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

# Write-side vocabulary (verified against the pinned submodule's Pydantic models).
_SET_PRICE_TYPES: dict[OrderType, str] = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "Market",
    OrderType.MTL: "MP",  # market-price-to-limit
    OrderType.ATO: "ATO",
    OrderType.ATC: "ATC",
    OrderType.ICEBERG: "Limit",  # iceberg = Limit + icebergVol
}
_TFEX_PRICE_TYPES: dict[OrderType, str] = {
    OrderType.LIMIT: "Limit",
    OrderType.MARKET: "Market",
    OrderType.STOP: "Stop",
    OrderType.STOP_LIMIT: "Stop",
    OrderType.ICEBERG: "Limit",
}
_VALIDITY: dict[Tif, str] = {
    Tif.DAY: "Day",
    Tif.GTC: "GTC",
    Tif.IOC: "IOC",
    Tif.FOK: "FOK",
}
_SET_SIDES: dict[Side, str] = {Side.BUY: "Buy", Side.SELL: "Sell"}
_TFEX_SIDES: dict[Side, str] = {Side.BUY: "Long", Side.SELL: "Short"}
_POSITIONS: dict[PositionEffect, str] = {
    PositionEffect.OPEN: "Open",
    PositionEffect.CLOSE: "Close",
}

_TWO_DP = Decimal("0.01")


def place_path(market: Market) -> str:
    """Relative path (no leading slash — the transport joins under /api/v1)."""
    return "order/place/set" if market is Market.SET else "order/place/tfex"


def cancel_path(market: Market) -> str:
    return "order/cancelled/set" if market is Market.SET else "order/cancelled/tfex"


def orders_path(account: str) -> str:
    return f"orders/{account}"


def venue_side(side: Side, market: Market) -> str:
    """Write-side strings: SET Buy/Sell, TFEX Long/Short."""
    return _SET_SIDES[side] if market is Market.SET else _TFEX_SIDES[side]


def from_venue_side(raw: str) -> Side | None:
    """Read-side strings differ from the write side: ``B``/``S``."""
    token = raw.strip().upper()
    if token in ("B", "BUY", "LONG"):
        return Side.BUY
    if token in ("S", "SELL", "SHORT"):
        return Side.SELL
    return None


def venue_price_type(order_type: OrderType, market: Market) -> str:
    table = _SET_PRICE_TYPES if market is Market.SET else _TFEX_PRICE_TYPES
    mapped = table.get(order_type)
    if mapped is None:
        raise LiberatorMappingError(
            f"order_type {order_type} is not expressible on the Liberator {market} wire"
        )
    return mapped


def venue_validity(tif: Tif) -> str:
    return _VALIDITY[tif]


def _set_wire_price(order: NormalizedOrder) -> str:
    """SET requires price > 0 with ≤2 dp on EVERY order (upstream wire contract).

    A market-family order without any price cannot be expressed; a >2 dp price
    is never silently re-quantized (that would alter the limit) — both reject
    pre-flight with a durable reason.
    """
    price = order.price if order.price is not None else order.stop_price
    if price is None:
        raise LiberatorMappingError(
            "liberator SET wire requires price > 0 even for market-family orders; "
            "supply an indicative price"
        )
    if price != price.quantize(_TWO_DP):
        raise LiberatorMappingError(
            f"liberator SET wire allows at most 2 decimal places (got {price})"
        )
    return format(price.quantize(_TWO_DP), "f")


# ════════════════════════════════════════════════════════════════════════════
# 🔴 NOT A SECRET, AND NOT THE TRADING PIN. Do not set this from configuration.
#
# The bridge REQUIRES a `pin` field on all six order models — `Field(..., min_length=6,
# max_length=10)` plus a `field_validator` enforcing digits-only — so the engine cannot
# simply omit it: an absent or malformed value is a 422 and the order never reaches the
# venue. But the bridge then OVERWRITES it, unconditionally, at six sites
# (`pin = get_settings().liberator_pin` in place/cancel/pre-place of both the SET and TFEX
# services). The engine's value is therefore inert: it satisfies a schema and is discarded.
#
# EVIDENCE, because "the bridge overwrites it" is exactly the kind of claim that should not
# be taken on trust:
#   * AST over the deployed services: 6 unconditional overwrite sites, and ZERO reads of any
#     caller-supplied `.pin` attribute, against a control of 37-39 attribute reads on the
#     same request objects in the same files;
#   * the deployed bridge code was digest-matched to `origin/main` on both nodes, so the
#     overwrite that was read is the overwrite that runs;
#   * 🔑 production had already proven it: the engine's configured PIN was NOT the bridge's
#     PIN, and real orders — places AND cancels, both of which stamp this field — were
#     accepted by the venue throughout (2026-09-04: every order carried a venue handle).
#     A wrong PIN reaching the venue would have failed them all.
#
# Holding a real trading credential to fill a field that is thrown away made this engine a
# second custodian of a live secret for no benefit ([[TK-0529]]). Ten digits, all zeros, so
# it cannot be mistaken for a real PIN (both observed real PINs are six digits) and so this
# value never appears in any node's configuration.
#
# ➡️ The true fix is to make `pin` optional on the bridge, after which the engine sends
#    nothing at all. That is a bridge change and is deliberately not made here.
# ════════════════════════════════════════════════════════════════════════════
_BRIDGE_REQUIRED_PIN_PLACEHOLDER = "0000000000"


def to_set_payload(order: NormalizedOrder) -> dict[str, Any]:
    """``NormalizedOrder`` → ``SETOrderRequest`` JSON body."""
    return {
        "accountNo": order.account,
        "icebergVol": order.display_qty or 0,
        "volume": order.quantity,
        "symbol": order.symbol,
        "side": venue_side(order.side, Market.SET),
        "pin": _BRIDGE_REQUIRED_PIN_PLACEHOLDER,
        "price": _set_wire_price(order),
        "priceType": venue_price_type(order.order_type, Market.SET),
        "validityType": venue_validity(order.tif),
        "nvdr": False,
    }


def to_tfex_payload(order: NormalizedOrder) -> dict[str, Any]:
    """``NormalizedOrder`` → ``TFEXOrderRequest`` JSON body.

    ``stopSymbol`` is required by the upstream model on EVERY TFEX order — it
    defaults to the order symbol. ``stopCondition`` ships empty in v1: the
    venue's condition vocabulary is undocumented upstream and is pinned during
    operator-driven micro_live validation (a venue reject flows back typed,
    never silent). TFEX ``price`` accepts 0 (market/stop-market orders).
    """
    if order.position_effect is None:  # pragma: no cover - contract guarantees it
        raise LiberatorMappingError("TFEX orders require position_effect")
    price = order.price if order.price is not None else Decimal("0")
    is_stop = order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
    stop_price = order.stop_price if is_stop and order.stop_price is not None else Decimal("0")
    return {
        "accountNo": order.account,
        "icebergVol": order.display_qty or 0,
        "volume": order.quantity,
        "symbol": order.symbol,
        "side": venue_side(order.side, Market.TFEX),
        "position": _POSITIONS[order.position_effect],
        "pin": _BRIDGE_REQUIRED_PIN_PLACEHOLDER,
        "price": format(price, "f"),
        "priceType": venue_price_type(order.order_type, Market.TFEX),
        "validityType": venue_validity(order.tif),
        "stopCondition": "",
        "stopSymbol": order.symbol,
        "stopPrice": format(stop_price, "f"),
    }


def to_place_payload(order: NormalizedOrder) -> dict[str, Any]:
    if order.market is Market.SET:
        return to_set_payload(order)
    return to_tfex_payload(order)


def to_cancel_payload(broker_order_id: str) -> dict[str, Any]:
    """Cancel is by venue order number, as a list (≤50 — the engine sends one)."""
    return {"orderNo": [broker_order_id], "pin": _BRIDGE_REQUIRED_PIN_PLACEHOLDER}


# ----------------------------------------------------------------- read side

_READ_PRICE_TYPES: dict[str, OrderType] = {
    "LIMIT": OrderType.LIMIT,
    "MARKET": OrderType.MARKET,
    "MP": OrderType.MTL,
    "ATO": OrderType.ATO,
    "ATC": OrderType.ATC,
    "STOP": OrderType.STOP,
}
_READ_VALIDITY: dict[str, Tif] = {"DAY": Tif.DAY, "GTC": Tif.GTC, "IOC": Tif.IOC, "FOK": Tif.FOK}
_READ_POSITIONS: dict[str, PositionEffect] = {
    "OPEN": PositionEffect.OPEN,
    "CLOSE": PositionEffect.CLOSE,
}


def _synthetic_client_order_id(order_no: str) -> str:
    """Deterministic UUIDv4-shaped placeholder for an uncorrelated venue row.

    ``get_open_orders`` must return ``NormalizedOrder`` (frozen §D) but the
    venue does not know our client ids. The placeholder is a stable hash of
    the venue order number with the v4 version/variant bits set so it passes
    the contract validator; it is a READ-ONLY view id and is never persisted.
    """
    digest = bytearray(hashlib.sha256(f"liberator:{order_no}".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def venue_item_to_normalized(item: VenueOrderItem, *, account: str) -> NormalizedOrder | None:
    """Best-effort venue row → ``NormalizedOrder`` (read-only view, ADR §B).

    Rows the frozen contract cannot represent (unknown price type, non-positive
    limit price such as a negative futures-spread, TFEX ``Auto`` position)
    return ``None`` and are skipped by the caller — a read view never guesses.
    """
    side = from_venue_side(item.side)
    order_type = _READ_PRICE_TYPES.get(item.price_type.strip().upper())
    if side is None or order_type is None:
        return None
    if order_type is OrderType.LIMIT and item.iceberg_vol > 0:
        order_type = OrderType.ICEBERG
    position = _READ_POSITIONS.get((item.position or "").strip().upper())
    market = Market.TFEX if (item.position or "").strip() else Market.SET
    try:
        return NormalizedOrder(
            client_order_id=_synthetic_client_order_id(item.order_no),
            broker=Broker.LIBERATOR,
            account=item.account_no or account,
            market=market,
            symbol=item.symbol,
            side=side,
            order_type=order_type,
            price=item.price,
            stop_price=item.stop_price if order_type is OrderType.STOP else None,
            quantity=item.volume,
            display_qty=item.iceberg_vol if order_type is OrderType.ICEBERG else None,
            tif=_READ_VALIDITY.get(item.validity_type.strip().upper(), Tif.DAY),
            position_effect=position,
        )
    except ValueError:
        return None


class VenueOrderState(StrEnum):
    """Coarse venue classification driving the reconciler's transitions.

    Fills are NOT a state here — they are derived from the cumulative
    ``matched`` counter independently of this classification.
    """

    RESTING = "resting"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


_CANCELLED_STATUS = frozenset({"CANCELLED", "CANCELED", "CXL"})
_REJECTED_STATUS = frozenset({"REJECTED", "REJECT"})
_EXPIRED_STATUS = frozenset({"EXPIRED", "EXPIRE"})

# ── statusShow codes, from the VENUE'S OWN code→label dictionary ──────────────────
# Decoded 2026-08-25 from the public client bundle by ``session:lib-research``
# (positive control 6/6) — see the umbrella `docs/reference/liberator-order-plane.md`.
#
# 🔴 THREE DISTINCT CODES ALL MEAN *CANCELLED*: C, X, XC. Until 2026-08-25 this module
# mapped ``show == "X"`` to EXPIRED, which was simply wrong — and it is in production
# data: order 18439 was cancelled and is recorded EXPIRED ([[TK-0428]]).
_CANCELLED_SHOW = frozenset({"C", "X", "XC"})
_REJECTED_SHOW = frozenset({"R"})

# ⚠️ XA — TERMINAL, and the venue cannot name it either. It appears in both of the
# venue client's terminal sets (excluded from bulk cancel alongside Matched/Rejected/
# the three Cancelled codes) yet has NO entry in its label dictionary; rendered in the
# app's own Status column it would come out blank. So: terminal is CAPTURED, the
# flavour is INFERRED.
_TERMINAL_UNNAMED_SHOW = frozenset({"XA"})


def classify_venue_state(item: VenueOrderItem) -> VenueOrderState:
    """Map venue status/rejectCode/counters onto the coarse classification.

    Conservative by design: a non-empty ``rejectCode`` always wins; explicit
    status words match next (full words on ``status``, codes on ``statusShow``);
    a fully-cancelled counter triple is the numeric fallback; anything
    unrecognized is treated as RESTING (the reconciler then only acts on the
    ``matched`` counter — never guesses a terminal state).

    🔑 That conservative default is deliberately preserved. It is why the codes this
    venue emits but we had never mapped (``XC``, ``XA``) failed *safe* — reading as
    still-working rather than inventing a fill. The defect it could not catch was the
    one code that looked handled and was mapped **wrongly** (``X``).
    """
    if item.reject_code.strip():
        return VenueOrderState.REJECTED
    status = item.status.strip().upper()
    show = item.status_show.strip().upper()
    if status in _CANCELLED_STATUS or show in _CANCELLED_SHOW:
        return VenueOrderState.CANCELLED
    if status in _REJECTED_STATUS or show in _REJECTED_SHOW:
        return VenueOrderState.REJECTED
    if show in _TERMINAL_UNNAMED_SHOW:
        # Treated as CANCELLED on inference, and said out loud every time because the
        # inference is not evidence. It CANNOT cost a fill: ``plan_actions`` emits the
        # ``matched``-delta fill action BEFORE appending any terminal action, so the
        # label never gates fill capture. The alternative — leaving it RESTING — is
        # strictly worse: a venue-terminal order open forever, and any caller waiting
        # on terminality waits forever with it.
        logger.warning(
            "liberator venue status %r is TERMINAL but unnamed in the venue's own "
            "dictionary — treating as CANCELLED by inference (order_no=%s). If you are "
            "reading this, that inference now has evidence: record it.",
            show,
            item.order_no,
        )
        return VenueOrderState.CANCELLED
    if status in _EXPIRED_STATUS:
        # ⚠️ Retained as an INSTRUMENT, not as a live mapping. The venue's dictionary
        # contains no expiry code at all, so this should never fire; if it ever does,
        # the dictionary is incomplete and that is worth knowing. ``show == "X"`` was
        # removed from this branch — that was the bug.
        logger.warning(
            "liberator emitted an EXPIRY status word (%r) — the venue's decoded "
            "dictionary has no expiry code, so this contradicts it (order_no=%s)",
            status,
            item.order_no,
        )
        return VenueOrderState.EXPIRED
    if item.cancelled > 0 and item.balance == 0 and item.matched < item.volume:
        return VenueOrderState.CANCELLED
    return VenueOrderState.RESTING

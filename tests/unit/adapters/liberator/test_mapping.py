"""Field mapping: every valid (market, order_type, side, position_effect, tif) cell."""

from __future__ import annotations

from typing import Any

import pytest
from src.quant_execution_engine.adapters.liberator import mapping
from src.quant_execution_engine.adapters.liberator.errors import LiberatorMappingError
from src.quant_execution_engine.adapters.liberator.models import VenueOrderItem
from src.quant_execution_engine.contracts.enums import (
    Market,
    OrderType,
    PositionEffect,
    Side,
    Tif,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder

from tests.conftest import make_order

_SET_TYPES = ("MARKET", "LIMIT", "ICEBERG", "MTL", "ATO", "ATC")
_TFEX_TYPES = ("MARKET", "LIMIT", "STOP", "STOP_LIMIT", "ICEBERG")
_TIFS = ("DAY", "GTC", "IOC", "FOK")
_EXPECTED_SET_PRICE_TYPE = {
    "MARKET": "Market",
    "LIMIT": "Limit",
    "ICEBERG": "Limit",
    "MTL": "MP",
    "ATO": "ATO",
    "ATC": "ATC",
}
_EXPECTED_TFEX_PRICE_TYPE = {
    "MARKET": "Market",
    "LIMIT": "Limit",
    "STOP": "Stop",
    "STOP_LIMIT": "Stop",
    "ICEBERG": "Limit",
}
_EXPECTED_VALIDITY = {"DAY": "Day", "GTC": "GTC", "IOC": "IOC", "FOK": "FOK"}


def _valid_order(
    market: str, order_type: str, side: str, tif: str, **extra: Any
) -> NormalizedOrder:
    """A contract-valid order for any (market, order_type) cell."""
    kwargs: dict[str, Any] = {
        "broker": "liberator",
        "market": market,
        "order_type": order_type,
        "side": side,
        "tif": tif,
        "price": "123.45",
        "quantity": 100,
    }
    if market == "TFEX":
        kwargs["symbol"] = "S50H26"
        kwargs["position_effect"] = "OPEN"
    if order_type in ("STOP", "STOP_LIMIT"):
        kwargs["stop_price"] = "120.50"
    if order_type == "ICEBERG":
        kwargs["display_qty"] = 10
    kwargs.update(extra)
    return make_order(**kwargs)


@pytest.mark.parametrize("tif", _TIFS)
@pytest.mark.parametrize("side", ("BUY", "SELL"))
@pytest.mark.parametrize("order_type", _SET_TYPES)
def test_set_payload_every_valid_cell(order_type: str, side: str, tif: str) -> None:
    order = _valid_order("SET", order_type, side, tif)
    payload = mapping.to_set_payload(order, pin="123456")
    assert payload == {
        "accountNo": order.account,
        "icebergVol": order.display_qty or 0,
        "volume": 100,
        "symbol": order.symbol,
        "side": "Buy" if side == "BUY" else "Sell",
        "pin": "123456",
        "price": "123.45",
        "priceType": _EXPECTED_SET_PRICE_TYPE[order_type],
        "validityType": _EXPECTED_VALIDITY[tif],
        "nvdr": False,
    }


@pytest.mark.parametrize("tif", _TIFS)
@pytest.mark.parametrize("position_effect", ("OPEN", "CLOSE"))
@pytest.mark.parametrize("side", ("BUY", "SELL"))
@pytest.mark.parametrize("order_type", _TFEX_TYPES)
def test_tfex_payload_every_valid_cell(
    order_type: str, side: str, position_effect: str, tif: str
) -> None:
    order = _valid_order("TFEX", order_type, side, tif, position_effect=position_effect)
    payload = mapping.to_tfex_payload(order, pin="654321")
    is_stop = order_type in ("STOP", "STOP_LIMIT")
    assert payload == {
        "accountNo": order.account,
        "icebergVol": order.display_qty or 0,
        "volume": 100,
        "symbol": "S50H26",
        "side": "Long" if side == "BUY" else "Short",
        "position": "Open" if position_effect == "OPEN" else "Close",
        "pin": "654321",
        "price": "123.45",
        "priceType": _EXPECTED_TFEX_PRICE_TYPE[order_type],
        "validityType": _EXPECTED_VALIDITY[tif],
        "stopCondition": "",
        "stopSymbol": "S50H26",
        "stopPrice": "120.50" if is_stop else "0",
    }


def test_iceberg_maps_display_qty_to_iceberg_vol() -> None:
    order = _valid_order("SET", "ICEBERG", "BUY", "DAY", display_qty=25)
    assert mapping.to_set_payload(order, pin="123456")["icebergVol"] == 25
    tfex = _valid_order("TFEX", "ICEBERG", "SELL", "DAY", display_qty=7)
    assert mapping.to_tfex_payload(tfex, pin="123456")["icebergVol"] == 7


def test_tfex_market_order_without_price_sends_zero() -> None:
    order = _valid_order("TFEX", "MARKET", "BUY", "IOC", price=None)
    payload = mapping.to_tfex_payload(order, pin="123456")
    assert payload["price"] == "0"


def test_set_market_family_without_any_price_rejects_preflight() -> None:
    order = _valid_order("SET", "MARKET", "BUY", "DAY", price=None)
    with pytest.raises(LiberatorMappingError, match="indicative price"):
        mapping.to_set_payload(order, pin="123456")


def test_set_price_more_than_two_dp_rejects_never_requantizes() -> None:
    order = _valid_order("SET", "LIMIT", "BUY", "DAY", price="123.456")
    with pytest.raises(LiberatorMappingError, match="2 decimal places"):
        mapping.to_set_payload(order, pin="123456")


def test_set_stop_types_are_not_expressible() -> None:
    with pytest.raises(LiberatorMappingError, match="not expressible"):
        mapping.venue_price_type(OrderType.STOP, Market.SET)
    with pytest.raises(LiberatorMappingError, match="not expressible"):
        mapping.venue_price_type(OrderType.STOP_LIMIT, Market.SET)


def test_tfex_auction_and_mtl_types_are_not_expressible() -> None:
    for order_type in (OrderType.ATO, OrderType.ATC, OrderType.MTL):
        with pytest.raises(LiberatorMappingError, match="not expressible"):
            mapping.venue_price_type(order_type, Market.TFEX)


def test_place_payload_dispatches_by_market() -> None:
    set_order = _valid_order("SET", "LIMIT", "BUY", "DAY")
    tfex_order = _valid_order("TFEX", "LIMIT", "BUY", "DAY")
    assert "nvdr" in mapping.to_place_payload(set_order, pin="1" * 6)
    assert "position" in mapping.to_place_payload(tfex_order, pin="1" * 6)


def test_paths_are_relative_without_leading_slash() -> None:
    assert mapping.place_path(Market.SET) == "order/place/set"
    assert mapping.place_path(Market.TFEX) == "order/place/tfex"
    assert mapping.cancel_path(Market.SET) == "order/cancelled/set"
    assert mapping.cancel_path(Market.TFEX) == "order/cancelled/tfex"
    assert mapping.orders_path("70173292") == "orders/70173292"


def test_cancel_payload_is_single_element_order_no_list() -> None:
    assert mapping.to_cancel_payload("3064", pin="123456") == {
        "orderNo": ["3064"],
        "pin": "123456",
    }


def test_venue_side_round_trip() -> None:
    assert mapping.venue_side(Side.BUY, Market.SET) == "Buy"
    assert mapping.venue_side(Side.SELL, Market.SET) == "Sell"
    assert mapping.venue_side(Side.BUY, Market.TFEX) == "Long"
    assert mapping.venue_side(Side.SELL, Market.TFEX) == "Short"
    assert mapping.from_venue_side("B") is Side.BUY
    assert mapping.from_venue_side("s") is Side.SELL
    assert mapping.from_venue_side("Long") is Side.BUY
    assert mapping.from_venue_side("Short") is Side.SELL
    assert mapping.from_venue_side("??") is None


def _item(**overrides: Any) -> VenueOrderItem:
    payload: dict[str, Any] = {
        "orderNo": "3064",
        "accountNo": "70173292",
        "symbol": "PTT",
        "side": "B",
        "volume": 100,
        "matched": 0,
        "balance": 100,
        "cancelled": 0,
        "status": "PENDING",
        "statusShow": "O",
        "rejectCode": "",
    }
    payload.update(overrides)
    return VenueOrderItem.model_validate(payload)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, mapping.VenueOrderState.RESTING),
        ({"status": "Matched", "matched": 100, "balance": 0}, mapping.VenueOrderState.RESTING),
        ({"rejectCode": "RJ-105"}, mapping.VenueOrderState.REJECTED),
        ({"status": "REJECTED"}, mapping.VenueOrderState.REJECTED),
        ({"statusShow": "R"}, mapping.VenueOrderState.REJECTED),
        ({"status": "CANCELLED"}, mapping.VenueOrderState.CANCELLED),
        ({"status": "Canceled"}, mapping.VenueOrderState.CANCELLED),
        ({"statusShow": "C"}, mapping.VenueOrderState.CANCELLED),
        # 🔴 TK-0428: `X` used to assert EXPIRED here. The venue's own dictionary says
        # X = Cancelled, and it has NO expiry code at all — so this row was pinning
        # the bug in place. All THREE of the venue's cancel codes now assert CANCELLED.
        ({"statusShow": "X"}, mapping.VenueOrderState.CANCELLED),
        ({"statusShow": "XC"}, mapping.VenueOrderState.CANCELLED),
        # XA: terminal per the venue's own cancel-exclusion set, unnamed in its
        # dictionary. Mapped by inference; leaving it RESTING would strand a
        # venue-terminal order open forever.
        ({"statusShow": "XA"}, mapping.VenueOrderState.CANCELLED),
        # The status WORD branch survives as an instrument — the venue is not believed
        # to emit it, and if it ever does we want to know rather than silently cope.
        ({"status": "EXPIRED"}, mapping.VenueOrderState.EXPIRED),
        (
            {"cancelled": 100, "balance": 0, "matched": 0, "status": "whatever"},
            mapping.VenueOrderState.CANCELLED,
        ),
        ({"status": "SOMETHING_NEW"}, mapping.VenueOrderState.RESTING),
    ],
)
def test_classify_venue_state(overrides: dict[str, Any], expected: mapping.VenueOrderState) -> None:
    assert mapping.classify_venue_state(_item(**overrides)) is expected


def test_reject_code_wins_over_status_words() -> None:
    item = _item(rejectCode="105", status="CANCELLED")
    assert mapping.classify_venue_state(item) is mapping.VenueOrderState.REJECTED


def test_position_effect_mapping_is_exact() -> None:
    order = _valid_order("TFEX", "LIMIT", "BUY", "DAY", position_effect="CLOSE")
    assert mapping.to_tfex_payload(order, pin="123456")["position"] == "Close"
    assert mapping._POSITIONS[PositionEffect.OPEN] == "Open"


def test_validity_mapping_is_exhaustive() -> None:
    assert {mapping.venue_validity(t) for t in Tif} == {"Day", "GTC", "IOC", "FOK"}

"""TFEX derivatives wire mapping + read-side: every declared cell + classify."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from src.quant_execution_engine.adapters.settrade import mapping
from src.quant_execution_engine.adapters.settrade.errors import SettradeMappingError
from src.quant_execution_engine.adapters.settrade.models import SettradeOrderItem
from src.quant_execution_engine.contracts.enums import Market, OrderType, PositionEffect, Side, Tif
from src.quant_execution_engine.contracts.orders import NormalizedOrder

from tests.conftest import make_order

_TFEX_TYPES = ("MARKET", "LIMIT", "MTL", "ATO", "STOP", "STOP_LIMIT", "ICEBERG")
_TIFS = ("DAY", "GTC", "IOC", "FOK")
_EXPECTED_PRICE_TYPE = {
    "MARKET": "MP-MKT",
    "LIMIT": "Limit",
    "MTL": "MP-MTL",
    "ATO": "ATO",
    "STOP": "MP-MKT",
    "STOP_LIMIT": "Limit",
    "ICEBERG": "Limit",
}
_EXPECTED_VALIDITY = {"DAY": "Day", "GTC": "Cancel", "IOC": "IOC", "FOK": "FOK"}
_PRICE_ZERO = {"MARKET", "MTL", "ATO", "STOP"}
_STOP = {"STOP", "STOP_LIMIT"}


def _tfex_order(
    order_type: str, side: str, position_effect: str, tif: str, **extra: Any
) -> NormalizedOrder:
    kwargs: dict[str, Any] = {
        "broker": "settrade",
        "market": "TFEX",
        "symbol": "S50H26",
        "order_type": order_type,
        "side": side,
        "tif": tif,
        "price": "800.50",
        "quantity": 5,
        "position_effect": position_effect,
    }
    if order_type in _STOP:
        kwargs["stop_price"] = "795.00"
    if order_type == "ICEBERG":
        kwargs["display_qty"] = 2
    kwargs.update(extra)
    return make_order(**kwargs)


@pytest.mark.parametrize("tif", _TIFS)
@pytest.mark.parametrize("position_effect", ("OPEN", "CLOSE"))
@pytest.mark.parametrize("side", ("BUY", "SELL"))
@pytest.mark.parametrize("order_type", _TFEX_TYPES)
def test_tfex_payload_every_declared_cell(
    order_type: str, side: str, position_effect: str, tif: str
) -> None:
    order = _tfex_order(order_type, side, position_effect, tif)
    payload = mapping.to_place_payload(order, pin="654321")
    expected: dict[str, Any] = {
        "symbol": "S50H26",
        "side": "Long" if side == "BUY" else "Short",
        "position": "Open" if position_effect == "OPEN" else "Close",
        "priceType": _EXPECTED_PRICE_TYPE[order_type],
        "price": 0 if order_type in _PRICE_ZERO else 800.5,
        "volume": 5,
        "validityType": _EXPECTED_VALIDITY[tif],
        "pin": "654321",
    }
    if order_type == "ICEBERG":
        expected["icebergVol"] = 2
    if order_type in _STOP:
        expected["stopCondition"] = "LAST_PAID_OR_HIGHER" if side == "BUY" else "LAST_PAID_OR_LOWER"
        expected["stopSymbol"] = "S50H26"
        expected["stopPrice"] = 795.0
    assert payload == expected


def test_stop_market_leg_sends_zero_price_with_stop_trio() -> None:
    buy = _tfex_order("STOP", "BUY", "OPEN", "DAY")
    payload = mapping.to_place_payload(buy, pin="654321")
    assert payload["price"] == 0
    assert payload["priceType"] == "MP-MKT"
    assert payload["stopCondition"] == "LAST_PAID_OR_HIGHER"
    assert payload["stopSymbol"] == "S50H26"
    assert payload["stopPrice"] == 795.0
    sell = _tfex_order("STOP", "SELL", "CLOSE", "DAY")
    assert mapping.to_place_payload(sell, pin="654321")["stopCondition"] == "LAST_PAID_OR_LOWER"


def test_stop_limit_sends_limit_price_and_stop_trio() -> None:
    order = _tfex_order("STOP_LIMIT", "BUY", "OPEN", "DAY")
    payload = mapping.to_place_payload(order, pin="654321")
    assert payload["priceType"] == "Limit"
    assert payload["price"] == 800.5
    assert payload["stopPrice"] == 795.0
    assert payload["stopCondition"] == "LAST_PAID_OR_HIGHER"


def test_missing_position_effect_raises() -> None:
    order = _tfex_order("LIMIT", "BUY", "OPEN", "DAY")
    bad = order.model_copy(update={"position_effect": None})
    with pytest.raises(SettradeMappingError, match="position_effect"):
        mapping.to_place_payload(bad, pin="654321")


def test_missing_stop_price_raises() -> None:
    order = _tfex_order("STOP", "BUY", "OPEN", "DAY")
    bad = order.model_copy(update={"stop_price": None})
    with pytest.raises(SettradeMappingError, match="stop_price"):
        mapping.to_place_payload(bad, pin="654321")


def test_gtc_maps_to_cancel() -> None:
    order = _tfex_order("LIMIT", "BUY", "OPEN", "GTC")
    assert mapping.to_place_payload(order, pin="654321")["validityType"] == "Cancel"


def test_iceberg_carries_iceberg_vol() -> None:
    order = _tfex_order("ICEBERG", "SELL", "CLOSE", "DAY", display_qty=3)
    assert mapping.to_place_payload(order, pin="654321")["icebergVol"] == 3


def test_wire_order_no_int_coercion_and_non_numeric_raises() -> None:
    assert mapping.wire_order_no(Market.TFEX, "123456") == 123456
    assert isinstance(mapping.wire_order_no(Market.TFEX, "123456"), int)
    with pytest.raises(SettradeMappingError, match="numeric"):
        mapping.wire_order_no(Market.TFEX, "ORD-ABC")


def test_change_cancel_bulk_payloads_tfex() -> None:
    assert mapping.to_change_payload(
        Market.TFEX, pin="654321", new_price=Decimal("810.00"), new_qty=7
    ) == {"pin": "654321", "newPrice": 810.0, "newVolume": 7}
    assert mapping.to_cancel_payload("654321") == {"pin": "654321"}
    assert mapping.to_bulk_cancel_payload("654321", ["123", "456"], Market.TFEX) == {
        "pin": "654321",
        "orders": [123, 456],
    }


def test_derivatives_paths_on_v3() -> None:
    base = "api/seosd/v3/001/accounts/0002"
    assert mapping.orders_path("001", "0002", Market.TFEX) == f"{base}/orders"
    assert mapping.order_path("001", "0002", Market.TFEX, "55") == f"{base}/orders/55"
    assert mapping.change_path("001", "0002", Market.TFEX, "55") == f"{base}/orders/55/change"
    assert mapping.trades_path("001", "0002", Market.TFEX) == f"{base}/trades"


# ------------------------------------------------------------------- read side


def _deriv_item(**overrides: Any) -> SettradeOrderItem:
    payload: dict[str, Any] = {
        "orderNo": 70123,
        "accountNo": "0002",
        "symbol": "S50H26",
        "side": "Long",
        "position": "Open",
        "priceType": "Limit",
        "price": 800.5,
        "qty": 5,
        "matchQty": 0,
        "balanceQty": 5,
        "icebergVol": 0,
        "status": "O",
        "showStatus": "Open(O)",
        "validity": "Day",
        "rejectCode": 0,
    }
    payload.update(overrides)
    return SettradeOrderItem.model_validate(payload)


def _equity_item(**overrides: Any) -> SettradeOrderItem:
    payload: dict[str, Any] = {
        "orderNo": "ORD-1",
        "accountNo": "0001",
        "symbol": "PTT",
        "side": "Buy",
        "priceType": "Limit",
        "price": 68.5,
        "vol": 100,
        "matched": 0,
        "balance": 100,
        "icebergVol": 0,
        "status": "O",
        "showOrderStatus": "Open(O)",
        "validity": "Day",
        "rejectCode": 0,
    }
    payload.update(overrides)
    return SettradeOrderItem.model_validate(payload)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (_deriv_item(), mapping.VenueOrderState.RESTING),
        (_deriv_item(status="E", showStatus="Expired(E)"), mapping.VenueOrderState.EXPIRED),
        (
            _equity_item(status="CS", showOrderStatus="Cancelled(CS)"),
            mapping.VenueOrderState.CANCELLED,
        ),
        (_deriv_item(rejectCode=105), mapping.VenueOrderState.REJECTED),
        (_deriv_item(rejectReason="price band"), mapping.VenueOrderState.REJECTED),
        (_deriv_item(status="SOMETHING_NEW"), mapping.VenueOrderState.RESTING),
    ],
)
def test_classify_venue_state(item: SettradeOrderItem, expected: mapping.VenueOrderState) -> None:
    assert mapping.classify_venue_state(item) is expected


def test_reject_code_wins_over_status_words() -> None:
    item = _deriv_item(rejectCode=105, status="Open")
    assert mapping.classify_venue_state(item) is mapping.VenueOrderState.REJECTED


def test_from_venue_side() -> None:
    assert mapping.from_venue_side("Buy") is Side.BUY
    assert mapping.from_venue_side("Long") is Side.BUY
    assert mapping.from_venue_side("b") is Side.BUY
    assert mapping.from_venue_side("Sell") is Side.SELL
    assert mapping.from_venue_side("Short") is Side.SELL
    assert mapping.from_venue_side("S") is Side.SELL
    assert mapping.from_venue_side("??") is None


def test_venue_item_to_normalized_derivatives_round_trip() -> None:
    item = _deriv_item()
    order = mapping.venue_item_to_normalized(item, account="0002", market=Market.TFEX)
    assert order is not None
    assert order.broker.value == "settrade"
    assert order.market is Market.TFEX
    assert order.symbol == "S50H26"
    assert order.side is Side.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.position_effect is PositionEffect.OPEN
    assert order.tif is Tif.DAY
    assert order.price == Decimal("800.5")
    assert order.quantity == 5


def test_venue_item_to_normalized_equity_round_trip() -> None:
    item = _equity_item()
    order = mapping.venue_item_to_normalized(item, account="0001", market=Market.SET)
    assert order is not None
    assert order.market is Market.SET
    assert order.symbol == "PTT"
    assert order.side is Side.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.position_effect is None
    assert order.quantity == 100


def test_venue_item_iceberg_classification() -> None:
    item = _equity_item(icebergVol=20)
    order = mapping.venue_item_to_normalized(item, account="0001", market=Market.SET)
    assert order is not None
    assert order.order_type is OrderType.ICEBERG
    assert order.display_qty == 20


def test_venue_item_unmappable_returns_none() -> None:
    # Unmappable price type -> skip.
    assert (
        mapping.venue_item_to_normalized(
            _deriv_item(priceType="WEIRD"), account="0002", market=Market.TFEX
        )
        is None
    )
    # Unknown validity -> skip.
    assert (
        mapping.venue_item_to_normalized(
            _deriv_item(validity="Date"), account="0002", market=Market.TFEX
        )
        is None
    )
    # TFEX row with an unclassifiable position -> skip.
    assert (
        mapping.venue_item_to_normalized(
            _deriv_item(position="Auto"), account="0002", market=Market.TFEX
        )
        is None
    )


def test_synthetic_cid_is_deterministic_uuid4() -> None:
    first = mapping._synthetic_client_order_id("70123")
    second = mapping._synthetic_client_order_id("70123")
    assert first == second
    parsed = uuid.UUID(first)
    assert parsed.version == 4
    assert mapping._synthetic_client_order_id("70124") != first

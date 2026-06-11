"""SET equity wire mapping: every declared SETTRADE×SET capability cell."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from src.quant_execution_engine.adapters.settrade import mapping
from src.quant_execution_engine.adapters.settrade.errors import SettradeMappingError
from src.quant_execution_engine.contracts.enums import Market, OrderType
from src.quant_execution_engine.contracts.orders import NormalizedOrder

from tests.conftest import make_order

_SET_TYPES = ("MARKET", "LIMIT", "MTL", "ATO", "ATC", "ICEBERG")
_TIFS = ("DAY", "GTC", "IOC", "FOK")
_EXPECTED_PRICE_TYPE = {
    "MARKET": "MP-MKT",
    "LIMIT": "Limit",
    "MTL": "MP-MTL",
    "ATO": "ATO",
    "ATC": "ATC",
    "ICEBERG": "Limit",
}
_EXPECTED_VALIDITY = {"DAY": "Day", "GTC": "Cancel", "IOC": "IOC", "FOK": "FOK"}
# Price-0 family (ATO/ATC/MP-MTL/MP-MKT) sends literal 0; priced types send the float.
_PRICE_ZERO = {"MARKET", "MTL", "ATO", "ATC"}


def _set_order(order_type: str, side: str, tif: str, **extra: Any) -> NormalizedOrder:
    kwargs: dict[str, Any] = {
        "broker": "settrade",
        "market": "SET",
        "symbol": "PTT",
        "order_type": order_type,
        "side": side,
        "tif": tif,
        "price": "68.50",
        "quantity": 100,
    }
    if order_type == "ICEBERG":
        kwargs["display_qty"] = 10
    kwargs.update(extra)
    return make_order(**kwargs)


@pytest.mark.parametrize("tif", _TIFS)
@pytest.mark.parametrize("side", ("BUY", "SELL"))
@pytest.mark.parametrize("order_type", _SET_TYPES)
def test_set_payload_every_declared_cell(order_type: str, side: str, tif: str) -> None:
    order = _set_order(order_type, side, tif)
    payload = mapping.to_place_payload(order, pin="123456")
    expected_price: int | float = 0 if order_type in _PRICE_ZERO else 68.5
    assert payload == {
        "pin": "123456",
        "side": "Buy" if side == "BUY" else "Sell",
        "symbol": "PTT",
        "trusteeIdType": "Local",
        "volume": 100,
        "qtyOpen": 10 if order_type == "ICEBERG" else 0,
        "price": expected_price,
        "priceType": _EXPECTED_PRICE_TYPE[order_type],
        "validityType": _EXPECTED_VALIDITY[tif],
        "clientType": "Individual",
    }


def test_iceberg_qty_open_carries_display_volume() -> None:
    order = _set_order("ICEBERG", "BUY", "DAY", display_qty=25)
    assert mapping.to_place_payload(order, pin="123456")["qtyOpen"] == 25


def test_wire_price_exactness_accept() -> None:
    assert mapping.wire_price(Decimal("68.50")) == 68.5
    assert mapping.wire_price(Decimal("123.45")) == 123.45
    assert mapping.wire_price(Decimal("1")) == 1.0


def test_wire_price_pathological_precision_rejects() -> None:
    with pytest.raises(SettradeMappingError, match="exactly"):
        mapping.wire_price(Decimal("123.45678901234567890123"))


def test_position_effect_on_set_rejects() -> None:
    order = _set_order("LIMIT", "BUY", "DAY", market="SET")
    # A SET order with a position_effect can't be built by the contract, so force
    # the mapping guard via model_construct-bypass: use the public path with a
    # contract-valid TFEX-shaped position smuggled in — assert the mapping raises.
    bad = order.model_copy(update={"position_effect": "OPEN"})
    with pytest.raises(SettradeMappingError, match="no position_effect"):
        mapping.to_place_payload(bad, pin="123456")


def test_set_stop_types_are_not_expressible() -> None:
    # STOP/STOP_LIMIT have no SET equity API — the capability gate rejects first,
    # but the mapping raises too (belt-and-braces). Start from a real SET order
    # (so dispatch stays on the equity book) and smuggle a STOP order_type past
    # the contract via a non-validating copy; the SET price-type table rejects it.
    for stop_type in (OrderType.STOP, OrderType.STOP_LIMIT):
        bad = _set_order("LIMIT", "BUY", "DAY").model_copy(update={"order_type": stop_type})
        with pytest.raises(SettradeMappingError, match="not expressible"):
            mapping.to_place_payload(bad, pin="123456")


def test_paths_relative_and_v4_trades() -> None:
    base = "api/seos/v3/001/accounts/0001"
    assert mapping.orders_path("001", "0001", Market.SET) == f"{base}/orders"
    assert mapping.order_path("001", "0001", Market.SET, "ORD1") == f"{base}/orders/ORD1"
    assert mapping.change_path("001", "0001", Market.SET, "ORD1") == f"{base}/orders/ORD1/change"
    assert mapping.cancel_path("001", "0001", Market.SET, "ORD1") == f"{base}/orders/ORD1/cancel"
    assert mapping.bulk_cancel_path("001", "0001", Market.SET) == f"{base}/cancel"
    assert mapping.account_info_path("001", "0001", Market.SET) == f"{base}/account-info"
    assert mapping.portfolios_path("001", "0001", Market.SET) == f"{base}/portfolios"
    # Equity trades live on v4.
    assert mapping.trades_path("001", "0001", Market.SET) == "api/seos/v4/001/accounts/0001/trades"


def test_set_order_no_passthrough_str() -> None:
    assert mapping.wire_order_no(Market.SET, "ORD-ABC") == "ORD-ABC"


def test_change_and_cancel_payloads() -> None:
    assert mapping.to_change_payload(
        Market.SET, pin="111111", new_price=Decimal("70.00"), new_qty=None
    ) == {"pin": "111111", "newPrice": 70.0}
    assert mapping.to_change_payload(Market.SET, pin="111111", new_price=None, new_qty=200) == {
        "pin": "111111",
        "newVolume": 200,
    }
    with pytest.raises(SettradeMappingError, match="new_price or new_qty"):
        mapping.to_change_payload(Market.SET, pin="111111", new_price=None, new_qty=None)
    assert mapping.to_cancel_payload("111111") == {"pin": "111111"}
    assert mapping.to_bulk_cancel_payload("111111", ["O1", "O2"], Market.SET) == {
        "pin": "111111",
        "orders": ["O1", "O2"],
    }

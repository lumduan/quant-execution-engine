"""Tolerant wire models: both-book order items, parse helpers, money exactness."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.quant_execution_engine.adapters.settrade.models import (
    SettradeAccountInfo,
    SettradeOrderItem,
    SettradePlaceResponse,
    SettradePortfolioItem,
    SettradeTokenResponse,
    SettradeTradeItem,
    parse_order_items,
)

# EXACT derivatives order example (from /tmp/settrade_docs/deriv/8_placeOrder.md).
DERIV_ORDER: dict[str, Any] = {
    "accountNo": "FT0017D",
    "balanceQty": 0,
    "canCancel": False,
    "canChange": False,
    "cancelQty": 1,
    "entryTime": "2022-08-31T14:37:04",
    "icebergVol": 0,
    "matchQty": 0,
    "orderNo": 1614803,
    "position": "Open",
    "price": 1299.0,
    "priceType": "Limit",
    "qty": 1,
    "rejectCode": 0,
    "rejectReason": None,
    "showStatus": "Expired(E)",
    "side": "Long",
    "status": "E",
    "statusMeaning": "Expired order",
    "symbol": "S50H23",
    "tradeDate": "2022-08-31",
    "validity": "Day",
    "version": 2,
}

# EXACT equity order example (from /tmp/settrade_docs/equity/4_getOrder.md).
EQUITY_ORDER: dict[str, Any] = {
    "accountNo": "FT0012E",
    "balance": 0,
    "canCancel": False,
    "cancelled": 100,
    "entryTime": "2022-08-30T18:31:08",
    "icebergVol": 0,
    "matched": 0,
    "nvdrFlag": " ",
    "orderNo": "27HUOO0IPT",
    "orderType": "Normal",
    "price": 68.0,
    "priceType": "Limit",
    "rejectCode": 0,
    "rejectReason": None,
    "showOrderStatus": "Cancelled(CS)",
    "showOrderStatusMeaning": "SETTRADE has confirmed offline order cancellation",
    "side": "Buy",
    "status": "CS",
    "symbol": "AOT",
    "tradeDate": "2022-08-30",
    "validity": "Day",
    "version": 1,
    "vol": 100,
}


def test_order_item_parses_exact_derivatives_example() -> None:
    item = SettradeOrderItem.model_validate(DERIV_ORDER)
    assert item.order_no == "1614803"  # int -> str
    assert item.account_no == "FT0017D"
    assert item.symbol == "S50H23"
    assert item.side == "Long"
    assert item.position == "Open"
    assert item.quantity == 1  # from "qty"
    assert item.matched == 0  # from "matchQty"
    assert item.balance == 0  # from "balanceQty"
    assert item.cancelled == 1  # from "cancelQty"
    assert item.status == "E"
    assert item.show_status == "Expired(E)"  # from "showStatus"
    assert item.status_meaning == "Expired order"  # from "statusMeaning"
    assert item.price == Decimal("1299.0")
    assert item.entry_time is not None
    assert not item.rejected


def test_order_item_parses_exact_equity_example() -> None:
    item = SettradeOrderItem.model_validate(EQUITY_ORDER)
    assert item.order_no == "27HUOO0IPT"  # already str
    assert item.account_no == "FT0012E"
    assert item.symbol == "AOT"
    assert item.side == "Buy"
    assert item.position is None  # equity has no position
    assert item.quantity == 100  # from "vol"
    assert item.matched == 0  # from "matched"
    assert item.balance == 0  # from "balance"
    assert item.cancelled == 100  # from "cancelled"
    assert item.status == "CS"
    assert item.show_status == "Cancelled(CS)"  # from "showOrderStatus"
    assert item.status_meaning.startswith("SETTRADE")  # from "showOrderStatusMeaning"
    assert item.price == Decimal("68.0")
    assert not item.rejected


def test_decimal_price_is_exact_via_str_round_trip() -> None:
    item = SettradeOrderItem.model_validate({"orderNo": "1", "price": 1299.0})
    assert item.price == Decimal("1299.0")
    assert str(item.price) == "1299.0"  # NOT a binary-float artefact
    blank = SettradeOrderItem.model_validate({"orderNo": "1", "price": ""})
    assert blank.price is None


def test_rejected_property_truth_table() -> None:
    def reject(**over: Any) -> bool:
        base: dict[str, Any] = {"orderNo": "1"}
        base.update(over)
        return SettradeOrderItem.model_validate(base).rejected

    assert reject(rejectCode=0) is False
    assert reject(rejectCode="0") is False
    assert reject(rejectCode="") is False
    assert reject(rejectCode=None) is False
    assert reject(rejectCode=42) is True
    assert reject(rejectCode="E07") is True
    assert reject(rejectCode=0, rejectReason="insufficient margin") is True


def test_parse_order_items_bare_list_dict_wrapper_and_garbage() -> None:
    bare = parse_order_items([DERIV_ORDER, EQUITY_ORDER])
    assert [i.order_no for i in bare] == ["1614803", "27HUOO0IPT"]
    assert parse_order_items({"data": [DERIV_ORDER]})[0].order_no == "1614803"
    assert parse_order_items({"orders": [EQUITY_ORDER]})[0].order_no == "27HUOO0IPT"
    assert parse_order_items({"unexpected": "shape"}) == []
    assert parse_order_items("garbage") == []
    assert parse_order_items(None) == []


def test_parse_order_items_skips_unparseable_rows(caplog: Any) -> None:
    import logging

    rows = [DERIV_ORDER, "not-a-dict", {"missing": "orderNo"}]
    with caplog.at_level(logging.WARNING):
        items = parse_order_items(rows)
    assert len(items) == 1  # only the valid deriv row survives
    assert items[0].order_no == "1614803"
    assert any("skipped" in r.getMessage() for r in caplog.records)


def test_token_response_model() -> None:
    token = SettradeTokenResponse.model_validate(
        {
            "token_type": "Bearer",
            "access_token": "atk",
            "refresh_token": "rtk",
            "expires_in": 1800,
            "extra": "ignored",
        }
    )
    assert token.token_type == "Bearer"
    assert token.expires_in == 1800


def test_place_response_normalizes_order_no_both_ways() -> None:
    assert SettradePlaceResponse.model_validate({"orderNo": 1614803}).order_no == "1614803"
    assert SettradePlaceResponse.model_validate({"orderNumber": "27ABC"}).order_no == "27ABC"


def test_account_info_tolerant_fields() -> None:
    equity = SettradeAccountInfo.model_validate(
        {"lineAvailable": 1978886.56, "excessEquity": 1003057.46, "equityBalance": 1004109.88}
    )
    assert equity.line_available == Decimal("1978886.56")
    assert equity.excess_equity == Decimal("1003057.46")
    assert equity.equity == Decimal("1004109.88")
    deriv = SettradeAccountInfo.model_validate({"excessEquity": 2000000000.0, "cashBalance": 2.0})
    assert deriv.excess_equity == Decimal("2000000000.0")
    assert deriv.cash_balance == Decimal("2.0")


def test_portfolio_item_net_quantity_both_books() -> None:
    equity = SettradePortfolioItem.model_validate({"symbol": "AOT", "currentVolume": 100})
    assert equity.symbol == "AOT"
    assert equity.net_quantity == 100
    deriv = SettradePortfolioItem.model_validate(
        {"symbol": "S50H23", "actualLongPosition": 21, "actualShortPosition": 5}
    )
    assert deriv.net_quantity == 16


def test_trade_item_model() -> None:
    trade = SettradeTradeItem.model_validate(
        {
            "orderNo": "27276QRZ81",
            "px": 70.0,
            "qty": 100,
            "tradeNo": "B-0AAC78420000005F",
            "side": "Buy",
            "tradeTime": "2025-03-11T10:29:39",
        }
    )
    assert trade.order_no == "27276QRZ81"
    assert trade.price == Decimal("70.0")
    assert trade.quantity == 100
    assert trade.trade_id == "B-0AAC78420000005F"  # from "tradeNo"
    assert trade.trade_time is not None

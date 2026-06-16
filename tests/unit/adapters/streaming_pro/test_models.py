"""Bridge wire models: BridgePlace predicate/reason + tolerant order-row parsing."""

from __future__ import annotations

from decimal import Decimal

from src.quant_execution_engine.adapters.streaming_pro.models import BridgePlace, parse_order_rows


def test_bridge_place_ok_and_order_no() -> None:
    place = BridgePlace.model_validate({"ok": True, "order_no": "8962991", "status": "Pending(S)"})
    assert place.ok and place.order_no == "8962991"


def test_bridge_place_reject_reason_priority() -> None:
    assert (
        "margin"
        in BridgePlace.model_validate({"ok": False, "reject_reason": "no margin"}).reject_reason()
    )
    assert "cap" in BridgePlace.model_validate({"detail": "cap"}).reject_reason()
    assert BridgePlace.model_validate({"ok": False}).reject_reason() == (
        "streaming_pro rejected the request"
    )


def test_parse_order_rows_from_bare_list() -> None:
    rows = parse_order_rows(
        [
            {
                "orderNo": "1",
                "symbol": "USDM26",
                "side": "Long",
                "qty": 1,
                "matchQty": 0,
                "balanceQty": 1,
                "extOrderNo": "E1",
                "entryTime": "2026-06-16T14:24:30",
            },
            "not-a-row",
        ]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.order_no == "1" and row.volume == 1 and row.matched == 0 and row.balance == 1
    assert row.ext_order_no == "E1" and row.entry_time is not None


def test_parse_order_rows_from_wrapped_dict_and_garbage() -> None:
    assert parse_order_rows({"orders": [{"orderNo": "9"}]})[0].order_no == "9"
    assert parse_order_rows({"portfolioList": [{"orderNo": "7"}]})[0].order_no == "7"
    assert parse_order_rows({"nope": 1}) == []
    assert parse_order_rows("garbage") == []


def test_venue_order_row_price_is_decimal() -> None:
    rows = parse_order_rows([{"orderNo": "1", "price": "32.47"}])
    assert rows[0].price == Decimal("32.47")

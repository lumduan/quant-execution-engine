"""Envelope success/reject parsing and OrderItem extraction."""

from __future__ import annotations

from typing import Any

from src.quant_execution_engine.adapters.liberator.models import (
    LiberatorEnvelope,
    parse_order_items,
)


def _envelope(**overrides: Any) -> LiberatorEnvelope:
    payload: dict[str, Any] = {
        "success": True,
        "message": "ok",
        "data": {"errorCode": 0, "errMsg": "", "result": {"orderNo": "3064"}},
    }
    payload.update(overrides)
    return LiberatorEnvelope.model_validate(payload)


def test_ok_requires_error_code_zero_and_no_err_msg() -> None:
    assert _envelope().ok
    assert not _envelope(success=False).ok
    assert not _envelope(data={"errorCode": 7, "errMsg": ""}).ok
    assert not _envelope(data={"errorCode": 0, "errMsg": "session expired"}).ok
    assert not _envelope(data=None).ok
    assert not _envelope(data={"errMsg": ""}).ok  # missing errorCode is never success


def test_reject_reason_is_always_non_empty_and_carries_venue_text() -> None:
    assert "errorCode=7" in _envelope(data={"errorCode": 7, "errMsg": ""}).reject_reason()
    reason = _envelope(data={"errorCode": 105, "errMsg": "insufficient balance"}).reject_reason()
    assert "errorCode=105" in reason and "insufficient balance" in reason
    assert (
        _envelope(success=False, data=None, message="upstream exploded").reject_reason()
        == "upstream exploded"
    )
    assert (
        _envelope(success=False, data=None, message=None, detail="Invalid API key").reject_reason()
        == "Invalid API key"
    )
    assert (
        _envelope(success=False, data=None, message=None).reject_reason()
        == "liberator rejected the request"
    )


def test_order_no_extraction() -> None:
    assert _envelope().order_no() == "3064"
    numeric = _envelope(data={"errorCode": 0, "errMsg": "", "result": {"orderNo": 3064}})
    assert numeric.order_no() == "3064"
    assert _envelope(data={"errorCode": 0, "errMsg": "", "result": {}}).order_no() is None
    assert _envelope(data={"errorCode": 0, "errMsg": "", "result": [1]}).order_no() is None
    assert _envelope(data=None).order_no() is None


def test_parse_order_items_happy_path_and_tolerance() -> None:
    payload = {
        "success": True,
        "data": {
            "errorCode": 0,
            "errMsg": "",
            "result": {
                "list": [
                    {
                        "orderNo": "3064",
                        "accountNo": "70173292",
                        "symbol": "S50U25",
                        "side": "S",
                        "volume": 2,
                        "matched": 1,
                        "balance": 1,
                        "cancelled": 0,
                        "status": "PENDING",
                        "statusShow": "O",
                        "rejectCode": "",
                        "price": "-0.50",
                        "entryTime": "2025-09-04T03:38:51.000Z",
                        "unknownUpstreamField": "ignored",
                    },
                    "not-a-dict-is-skipped",
                ]
            },
        },
    }
    items = parse_order_items(payload)
    assert len(items) == 1
    item = items[0]
    assert item.order_no == "3064"
    assert item.matched == 1
    assert str(item.price) == "-0.50"  # futures spreads price negative; Decimal preserved
    assert item.entry_time is not None and item.entry_time.tzinfo is not None


def test_parse_order_items_missing_levels_yield_empty() -> None:
    assert parse_order_items({}) == []
    assert parse_order_items({"data": None}) == []
    assert parse_order_items({"data": {"result": None}}) == []
    assert parse_order_items({"data": {"result": {"list": None}}}) == []
    assert parse_order_items({"data": {"result": {}}}) == []

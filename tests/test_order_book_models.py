"""OrderBook / OrderBookLevel model tests: Decimal round-trips + invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import (
    OrderBook,
    OrderBookLevel,
    OrderBookSource,
)


def _book(**overrides: object) -> OrderBook:
    payload: dict[str, object] = {
        "symbol": "AOT",
        "market": Market.SET,
        "bid_levels": [OrderBookLevel(price=Decimal("837.8"), volume=26)],
        "ask_levels": [OrderBookLevel(price=Decimal("838"), volume=24)],
        "sequence": 7,
        "source": OrderBookSource.SETTRADE,
        "received_at": datetime(2026, 6, 12, tzinfo=UTC),
    }
    payload.update(overrides)
    return OrderBook(**payload)  # type: ignore[arg-type]


def test_wire_dump_serializes_decimal_as_string() -> None:
    book = _book(
        bid_levels=[OrderBookLevel(price=Decimal("837.8"), volume=26)],
        ask_levels=[OrderBookLevel(price=Decimal("123.456789"), volume=1)],
    )
    dumped = book.wire_dump()
    assert dumped["bid_levels"][0]["price"] == "837.8"
    assert dumped["ask_levels"][0]["price"] == "123.456789"
    assert isinstance(dumped["bid_levels"][0]["price"], str)


def test_decimal_round_trip_is_byte_exact() -> None:
    for raw in ("837.8", "123.456789", "0.01", "999999.999999"):
        level = OrderBookLevel(price=Decimal(raw), volume=5)
        assert level.model_dump(mode="json")["price"] == raw
        assert level.price == Decimal(raw)


def test_best_bid_and_best_ask() -> None:
    book = _book(
        bid_levels=[
            OrderBookLevel(price=Decimal("837.8"), volume=26),
            OrderBookLevel(price=Decimal("837.7"), volume=36),
        ],
        ask_levels=[OrderBookLevel(price=Decimal("838"), volume=24)],
    )
    assert book.best_bid is not None and book.best_bid.price == Decimal("837.8")
    assert book.best_ask is not None and book.best_ask.price == Decimal("838")


def test_best_bid_ask_none_when_empty() -> None:
    book = _book(bid_levels=[], ask_levels=[])
    assert book.best_bid is None
    assert book.best_ask is None


def test_models_are_frozen() -> None:
    level = OrderBookLevel(price=Decimal("1"), volume=1)
    with pytest.raises(ValidationError):
        level.price = Decimal("2")
    book = _book()
    with pytest.raises(ValidationError):
        book.symbol = "PTT"


def test_volume_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        OrderBookLevel(price=Decimal("1"), volume=-1)


def test_flags_default_to_normal() -> None:
    book = _book()
    assert book.bid_flag == "NORMAL"
    assert book.ask_flag == "NORMAL"

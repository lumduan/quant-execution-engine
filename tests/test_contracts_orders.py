"""NormalizedOrder/Result contract: validators, Decimal-as-string, mapping."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from src.quant_execution_engine.contracts.enums import (
    Broker,
    OrderState,
    PublicOrderStatus,
    to_public_status,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrderResult

from tests.conftest import make_order


def test_minimal_limit_order_valid() -> None:
    order = make_order()
    assert order.price == Decimal("123.456789")
    assert order.metadata == {}


def test_client_order_id_must_be_uuid4() -> None:
    with pytest.raises(ValidationError, match="UUIDv4"):
        make_order(client_order_id="not-a-uuid")
    # a UUIDv1 is rejected too — version matters, not just shape
    with pytest.raises(ValidationError, match="UUIDv4"):
        make_order(client_order_id=str(uuid.uuid1()))


def test_price_rejects_float_input() -> None:
    with pytest.raises(ValidationError, match="never floats"):
        make_order(price=123.45)


@pytest.mark.parametrize(
    ("order_type", "missing"),
    [
        ("LIMIT", "price"),
        ("STOP_LIMIT", "price"),
        ("STOP", "stop_price"),
        ("STOP_LIMIT", "stop_price"),
    ],
)
def test_price_requiredness(order_type: str, missing: str) -> None:
    fields: dict[str, Any] = {"order_type": order_type, "price": "10", "stop_price": "9"}
    fields[missing] = None
    with pytest.raises(ValidationError, match=f"{missing} is required"):
        make_order(**fields)


def test_prices_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="price must be > 0"):
        make_order(price="0")
    with pytest.raises(ValidationError, match="stop_price must be > 0"):
        make_order(order_type="STOP", price=None, stop_price="-1")


def test_display_qty_iff_iceberg_and_capped() -> None:
    with pytest.raises(ValidationError, match="only valid for ICEBERG"):
        make_order(display_qty=10)
    with pytest.raises(ValidationError, match="require display_qty"):
        make_order(order_type="ICEBERG")
    with pytest.raises(ValidationError, match="<= quantity"):
        make_order(order_type="ICEBERG", display_qty=200, quantity=100)
    ok = make_order(order_type="ICEBERG", display_qty=10, quantity=100)
    assert ok.display_qty == 10


def test_position_effect_tfex_required_set_forbidden() -> None:
    with pytest.raises(ValidationError, match="required for TFEX"):
        make_order(market="TFEX", symbol="S50H26")
    with pytest.raises(ValidationError, match="omitted for SET"):
        make_order(position_effect="OPEN")
    ok = make_order(market="TFEX", symbol="S50H26", position_effect="OPEN")
    assert ok.position_effect is not None


def test_quantity_positive_and_extras_forbidden() -> None:
    with pytest.raises(ValidationError):
        make_order(quantity=0)
    with pytest.raises(ValidationError):
        make_order(unknown_field="x")


def test_result_decimal_as_string_and_raw_excluded() -> None:
    now = datetime.now(UTC)
    result = NormalizedOrderResult(
        client_order_id=str(uuid.uuid4()),
        broker_order_id="SIM-1",
        broker=Broker.SIM,
        status=PublicOrderStatus.FILLED,
        engine_state=OrderState.FILLED,
        filled_qty=100,
        remaining_qty=0,
        avg_fill_price=Decimal("123.456789"),
        reject_reason=None,
        created_at=now,
        updated_at=now,
        raw={"private": True},
    )
    wire = result.wire_dump()
    assert wire["avg_fill_price"] == "123.456789"
    assert isinstance(wire["avg_fill_price"], str)
    assert "raw" not in wire
    assert wire["engine_state"] == "FILLED"


@pytest.mark.parametrize(
    ("state", "filled", "expected"),
    [
        (OrderState.PENDING_NEW, 0, PublicOrderStatus.NEW),
        (OrderState.NEW, 0, PublicOrderStatus.NEW),
        (OrderState.PARTIALLY_FILLED, 40, PublicOrderStatus.PARTIALLY_FILLED),
        (OrderState.FILLED, 100, PublicOrderStatus.FILLED),
        (OrderState.PENDING_CANCEL, 0, PublicOrderStatus.NEW),
        (OrderState.PENDING_CANCEL, 40, PublicOrderStatus.PARTIALLY_FILLED),
        (OrderState.PENDING_REPLACE, 0, PublicOrderStatus.NEW),
        (OrderState.PENDING_REPLACE, 40, PublicOrderStatus.PARTIALLY_FILLED),
        (OrderState.CANCELLED, 0, PublicOrderStatus.CANCELLED),
        (OrderState.REJECTED, 0, PublicOrderStatus.REJECTED),
        (OrderState.EXPIRED, 0, PublicOrderStatus.EXPIRED),
    ],
)
def test_to_public_status_mapping(
    state: OrderState, filled: int, expected: PublicOrderStatus
) -> None:
    assert to_public_status(state, filled) is expected

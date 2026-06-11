"""LiberatorAdapter.place: ack parsing, status mapping, never-swallowed rejects."""

from __future__ import annotations

from typing import Any

import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
from src.quant_execution_engine.adapters.liberator.errors import LiberatorTransportError
from src.quant_execution_engine.adapters.liberator.transport import LiberatorTransport
from src.quant_execution_engine.contracts.enums import Broker

from tests.conftest import make_order

_BASE = "http://liberator-trading-api:8200/api/v1"


def make_adapter(**kwargs: Any) -> LiberatorAdapter:
    transport = LiberatorTransport(base_url=_BASE, api_key=SecretStr("test-key"))
    return LiberatorAdapter(transport=transport, pin=SecretStr("987654"), **kwargs)


def _ok_place(order_no: str = "3064") -> dict[str, Any]:
    return {
        "success": True,
        "message": "placed",
        "data": {"errorCode": 0, "errMsg": "", "result": {"orderNo": order_no}},
    }


def _liberator_order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "liberator", "price": "123.45"}
    payload.update(overrides)
    return make_order(**payload)


@respx.mock
async def test_place_set_routes_to_set_endpoint_and_acks() -> None:
    route = respx.post(f"{_BASE}/order/place/set").respond(json=_ok_place())
    adapter = make_adapter()
    ack = await adapter.place(_liberator_order())
    assert not ack.rejected
    assert ack.broker_order_id == "3064"
    assert ack.fills == ()  # fills arrive via reconciliation in v1
    sent = route.calls.last.request
    assert sent.headers["api-key"] == "test-key"
    await adapter.aclose()


@respx.mock
async def test_place_tfex_routes_to_tfex_endpoint() -> None:
    route = respx.post(f"{_BASE}/order/place/tfex").respond(json=_ok_place("9001"))
    adapter = make_adapter()
    order = _liberator_order(market="TFEX", symbol="S50H26", position_effect="OPEN", price="950.0")
    ack = await adapter.place(order)
    assert ack.broker_order_id == "9001"
    assert route.called
    await adapter.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("body", "expected_fragment"),
    [
        (
            {"success": True, "data": {"errorCode": 105, "errMsg": "insufficient balance"}},
            "insufficient balance",
        ),
        ({"success": True, "data": {"errorCode": 7, "errMsg": ""}}, "errorCode=7"),
        (
            {"success": False, "message": "session expired", "data": None},
            "session expired",
        ),
        ({"success": False}, "liberator rejected the request"),
    ],
)
async def test_place_venue_rejects_map_to_rejected_ack_with_reason(
    body: dict[str, Any], expected_fragment: str
) -> None:
    respx.post(f"{_BASE}/order/place/set").respond(json=body)
    adapter = make_adapter()
    ack = await adapter.place(_liberator_order())
    assert ack.rejected
    assert ack.reject_reason is not None and expected_fragment in ack.reject_reason
    await adapter.aclose()


@respx.mock
async def test_place_structured_4xx_is_rejected_ack_not_crash() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(
        status_code=401, json={"detail": "Invalid API key"}
    )
    adapter = make_adapter()
    ack = await adapter.place(_liberator_order())
    assert ack.rejected
    assert ack.reject_reason == "Invalid API key"
    await adapter.aclose()


@respx.mock
async def test_place_mapping_error_rejects_preflight_without_http() -> None:
    route = respx.post(f"{_BASE}/order/place/set").respond(json=_ok_place())
    adapter = make_adapter()
    ack = await adapter.place(_liberator_order(order_type="MARKET", price=None))
    assert ack.rejected
    assert ack.reject_reason is not None and "indicative price" in ack.reject_reason
    assert not route.called  # rejected before any venue I/O
    await adapter.aclose()


@respx.mock
async def test_place_missing_order_no_raises_adapter_error() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(
        json={"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}
    )
    adapter = make_adapter()
    with pytest.raises(AdapterError, match="orderNo"):
        await adapter.place(_liberator_order())
    await adapter.aclose()


@respx.mock
async def test_place_transport_failure_propagates_as_lost_ack_window() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(status_code=503)
    adapter = make_adapter()
    with pytest.raises(LiberatorTransportError):
        await adapter.place(_liberator_order())
    await adapter.aclose()


def test_capabilities_are_exactly_the_liberator_matrix_rows() -> None:
    adapter = make_adapter()
    rows = adapter.capabilities()
    assert {row.market.value for row in rows} == {"SET", "TFEX"}
    assert all(row.broker is Broker.LIBERATOR for row in rows)
    assert all(row.amend == "cancel_replace" for row in rows)

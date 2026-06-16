"""StreamingProAdapter.place: SET/TFEX routing, ack parsing, never-swallowed rejects, no PIN."""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.streaming_pro.adapter import StreamingProAdapter
from src.quant_execution_engine.adapters.streaming_pro.errors import StreamingProTransportError
from src.quant_execution_engine.adapters.streaming_pro.transport import StreamingProTransport
from src.quant_execution_engine.contracts.enums import Broker

from tests.conftest import make_order

_BASE = "http://streaming-pro-api:8000/api/v1"


def make_adapter(**kwargs: Any) -> StreamingProAdapter:
    transport = StreamingProTransport(base_url=_BASE, api_key=SecretStr("test-key"))
    return StreamingProAdapter(transport=transport, **kwargs)


def _sp_order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "streaming_pro", "price": "32.47"}
    payload.update(overrides)
    return make_order(**payload)


@respx.mock
async def test_place_set_routes_to_set_endpoint_and_acks() -> None:
    route = respx.post(f"{_BASE}/order/place/set").respond(
        json={"ok": True, "order_no": "71937953"}
    )
    adapter = make_adapter()
    ack = await adapter.place(_sp_order())
    assert not ack.rejected
    assert ack.broker_order_id == "71937953"
    assert ack.fills == ()  # fills arrive via reconciliation in v1
    sent = route.calls.last.request
    assert sent.headers["X-API-Key"] == "test-key"
    body = json.loads(sent.content)
    assert body["side"] == "BUY" and body["price"] == "32.47"
    assert "pin" not in body  # the bridge stamps the PIN, never the engine
    await adapter.aclose()


@respx.mock
async def test_place_tfex_routes_to_tfex_endpoint_with_position() -> None:
    route = respx.post(f"{_BASE}/order/place/tfex").respond(
        json={"ok": True, "order_no": "8962991", "status": "Pending(S)"}
    )
    adapter = make_adapter()
    order = _sp_order(market="TFEX", symbol="USDM26", position_effect="OPEN", price="32.47")
    ack = await adapter.place(order)
    assert ack.broker_order_id == "8962991"
    body = json.loads(route.calls.last.request.content)
    assert body["position"] == "OPEN"
    assert "pin" not in body
    await adapter.aclose()


@respx.mock
async def test_place_market_order_omits_price() -> None:
    route = respx.post(f"{_BASE}/order/place/set").respond(json={"ok": True, "order_no": "1"})
    adapter = make_adapter()
    await adapter.place(_sp_order(order_type="MARKET", price=None))
    body = json.loads(route.calls.last.request.content)
    assert "price" not in body  # bridge defaults a price-less market order to 0
    await adapter.aclose()


@respx.mock
@pytest.mark.parametrize(
    ("status_code", "body", "expected_fragment"),
    [
        (200, {"ok": False, "reject_reason": "Insufficient margin"}, "Insufficient margin"),
        (200, {"ok": False}, "streaming_pro rejected the request"),
        (422, {"detail": "volume 2 exceeds the bridge cap of 1"}, "exceeds the bridge cap"),
        (403, {"detail": "public mode: order endpoints disabled"}, "public mode"),
    ],
)
async def test_place_rejects_map_to_rejected_ack_with_reason(
    status_code: int, body: dict[str, Any], expected_fragment: str
) -> None:
    respx.post(f"{_BASE}/order/place/set").respond(status_code=status_code, json=body)
    adapter = make_adapter()
    ack = await adapter.place(_sp_order())
    assert ack.rejected
    assert ack.reject_reason is not None and expected_fragment in ack.reject_reason
    await adapter.aclose()


@respx.mock
async def test_place_mapping_error_rejects_preflight_without_http() -> None:
    route = respx.post(f"{_BASE}/order/place/tfex").respond(json={"ok": True, "order_no": "1"})
    adapter = make_adapter()
    # A TFEX order without position_effect cannot be expressed → rejected pre-flight (model_copy
    # bypasses the contract guard that requires position_effect for TFEX).
    order = _sp_order(market="TFEX", symbol="USDM26", position_effect="OPEN").model_copy(
        update={"position_effect": None}
    )
    ack = await adapter.place(order)
    assert ack.rejected
    assert ack.reject_reason is not None and "position_effect" in ack.reject_reason
    assert not route.called  # rejected before any venue I/O
    await adapter.aclose()


@respx.mock
async def test_place_transport_5xx_propagates_as_lost_ack_window() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(status_code=503)
    adapter = make_adapter()
    with pytest.raises(StreamingProTransportError):
        await adapter.place(_sp_order())
    await adapter.aclose()


def test_capabilities_are_exactly_the_streaming_pro_matrix_rows() -> None:
    adapter = make_adapter()
    rows = adapter.capabilities()
    assert {row.market.value for row in rows} == {"SET", "TFEX"}
    assert all(row.broker is Broker.STREAMING_PRO for row in rows)
    assert all(row.amend == "cancel_replace" for row in rows)

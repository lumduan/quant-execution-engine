"""StreamingProTransport: api-key header, 5xx/non-JSON → typed, 4xx passes through, redaction."""

from __future__ import annotations

import logging

import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.streaming_pro.errors import StreamingProTransportError
from src.quant_execution_engine.adapters.streaming_pro.transport import (
    StreamingProTransport,
    redact_payload,
)

_BASE = "http://streaming-pro-api:8000/api/v1"


def _transport() -> StreamingProTransport:
    return StreamingProTransport(
        base_url=_BASE + "/", api_key=SecretStr("k")
    )  # trailing-slash safe


@respx.mock
async def test_post_attaches_api_key_and_returns_body() -> None:
    route = respx.post(f"{_BASE}/order/place/set").respond(json={"ok": True, "order_no": "1"})
    t = _transport()
    body = await t.post("order/place/set", {"symbol": "PTT"})
    assert body == {"ok": True, "order_no": "1"}
    assert route.calls.last.request.headers["X-API-Key"] == "k"
    await t.aclose()


@respx.mock
async def test_post_4xx_returns_detail_body_not_error() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(status_code=422, json={"detail": "cap"})
    t = _transport()
    assert await t.post("order/place/set", {}) == {"detail": "cap"}  # structured, not breaker food
    await t.aclose()


@respx.mock
async def test_post_5xx_raises_typed() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(status_code=503)
    t = _transport()
    with pytest.raises(StreamingProTransportError):
        await t.post("order/place/set", {})
    await t.aclose()


@respx.mock
async def test_post_non_object_body_raises_typed() -> None:
    respx.post(f"{_BASE}/order/cancel").respond(json=[1, 2, 3])
    t = _transport()
    with pytest.raises(StreamingProTransportError, match="non-object"):
        await t.post("order/cancel", {})
    await t.aclose()


@respx.mock
async def test_get_json_returns_list_and_raises_on_non_json() -> None:
    respx.get(f"{_BASE}/orders").respond(json=[{"orderNo": "1"}])
    t = _transport()
    assert await t.get_json("orders") == [{"orderNo": "1"}]
    respx.get(f"{_BASE}/session/status").respond(
        content=b"<html/>", headers={"content-type": "text/html"}
    )
    with pytest.raises(StreamingProTransportError):
        await t.get_json("session/status")
    await t.aclose()


@respx.mock
async def test_get_json_5xx_raises_typed() -> None:
    respx.get(f"{_BASE}/portfolio").respond(status_code=502)
    t = _transport()
    with pytest.raises(StreamingProTransportError):
        await t.get_json("portfolio")
    await t.aclose()


@respx.mock
async def test_connectivity_error_raises_typed() -> None:
    import httpx

    respx.post(f"{_BASE}/order/place/set").side_effect = httpx.ConnectError
    t = _transport()
    with pytest.raises(StreamingProTransportError):
        await t.post("order/place/set", {})
    await t.aclose()


def test_redact_masks_account_and_pin() -> None:
    masked = redact_payload({"account": "9990099", "symbol": "USDM26", "pin": "1234"})
    assert masked["account"] == "***" and masked["pin"] == "***"
    assert masked["symbol"] == "USDM26"


@respx.mock
async def test_post_debug_log_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(f"{_BASE}/order/place/set").respond(json={"ok": True, "order_no": "1"})
    t = _transport()
    with caplog.at_level(logging.DEBUG):
        await t.post("order/place/set", {"account": "9990099", "symbol": "USDM26"})
    assert "9990099" not in caplog.text  # the account never reaches a log record
    await t.aclose()

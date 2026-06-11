"""Transport: base-url join, api-key header, typed failures, secret redaction."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.liberator.errors import LiberatorTransportError
from src.quant_execution_engine.adapters.liberator.transport import (
    LiberatorTransport,
    redact_payload,
)

_BASE = "http://liberator-trading-api:8200/api/v1"
_PIN = "987654"
_ACCOUNT = "70173292"


def _transport() -> LiberatorTransport:
    return LiberatorTransport(base_url=_BASE, api_key=SecretStr("test-key"))


def _ok_body() -> dict[str, object]:
    return {
        "success": True,
        "message": "ok",
        "data": {"errorCode": 0, "errMsg": "", "result": {"orderNo": "3064"}},
    }


@respx.mock
async def test_relative_path_join_preserves_api_v1_prefix() -> None:
    """Load-bearing: a leading-slash path would silently drop /api/v1."""
    route = respx.post(f"{_BASE}/order/place/set").respond(json=_ok_body())
    transport = _transport()
    envelope = await transport.post("order/place/set", {"pin": _PIN})
    assert envelope.ok
    assert route.called
    assert str(route.calls.last.request.url) == f"{_BASE}/order/place/set"
    # A defensive leading slash must land on the same URL, not replace the prefix.
    await transport.post("/order/place/set", {"pin": _PIN})
    assert str(route.calls.last.request.url) == f"{_BASE}/order/place/set"
    await transport.aclose()


@respx.mock
async def test_api_key_header_attached_from_secret() -> None:
    route = respx.get(f"{_BASE}/orders/{_ACCOUNT}").respond(json=_ok_body())
    transport = _transport()
    await transport.get_json(f"orders/{_ACCOUNT}")
    assert route.calls.last.request.headers["api-key"] == "test-key"
    await transport.aclose()


@respx.mock
async def test_5xx_and_connect_errors_raise_transport_error() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(status_code=503)
    transport = _transport()
    with pytest.raises(LiberatorTransportError, match="HTTP 503"):
        await transport.post("order/place/set", {"pin": _PIN})
    respx.get(f"{_BASE}/orders/{_ACCOUNT}").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(LiberatorTransportError, match="failed"):
        await transport.get_json(f"orders/{_ACCOUNT}")
    await transport.aclose()


@respx.mock
async def test_non_json_bodies_raise_transport_error() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(content=b"<html>boom</html>")
    respx.get(f"{_BASE}/orders/{_ACCOUNT}").respond(content=b"nope")
    transport = _transport()
    with pytest.raises(LiberatorTransportError, match="non-JSON"):
        await transport.post("order/place/set", {"pin": _PIN})
    with pytest.raises(LiberatorTransportError, match="non-JSON"):
        await transport.get_json(f"orders/{_ACCOUNT}")
    await transport.aclose()


@respx.mock
async def test_non_object_json_body_raises_transport_error() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(json=[1, 2, 3])
    transport = _transport()
    with pytest.raises(LiberatorTransportError, match="non-object"):
        await transport.post("order/place/set", {"pin": _PIN})
    await transport.aclose()


@respx.mock
async def test_structured_4xx_returns_envelope_with_reason() -> None:
    """A structured upstream rejection is venue truth, not a transport failure."""
    respx.post(f"{_BASE}/order/place/set").respond(
        status_code=401, json={"detail": "Invalid API key"}
    )
    transport = _transport()
    envelope = await transport.post("order/place/set", {"pin": _PIN})
    assert not envelope.ok
    assert envelope.reject_reason() == "Invalid API key"
    await transport.aclose()


def test_redact_payload_masks_secrets_case_insensitively() -> None:
    payload = {
        "pin": _PIN,
        "accountNo": _ACCOUNT,
        "account_no": _ACCOUNT,
        "password": "hunter2",
        "symbol": "PTT",
        "volume": 100,
    }
    redacted = redact_payload(payload)
    assert redacted["pin"] == "***"
    assert redacted["accountNo"] == "***"
    assert redacted["account_no"] == "***"
    assert redacted["password"] == "***"
    assert redacted["symbol"] == "PTT"
    assert redacted["volume"] == 100


@respx.mock
async def test_pin_and_account_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Hard rule 3: no log record may carry the PIN or the account number."""
    respx.post(f"{_BASE}/order/place/set").respond(json=_ok_body())
    transport = _transport()
    with caplog.at_level(logging.DEBUG):
        await transport.post(
            "order/place/set",
            {"pin": _PIN, "accountNo": _ACCOUNT, "symbol": "PTT", "volume": 100},
        )
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert _PIN not in rendered
    assert _ACCOUNT not in rendered
    assert "PTT" in rendered  # the redacted payload IS logged at DEBUG
    await transport.aclose()


async def test_injected_client_is_not_closed_by_transport() -> None:
    client = httpx.AsyncClient()
    transport = LiberatorTransport(base_url=_BASE, api_key=SecretStr("k"), client=client)
    await transport.aclose()
    assert not client.is_closed
    await client.aclose()

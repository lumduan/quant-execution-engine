"""Transport: response policy, typed failures, wire-health, rate budgets, secret hygiene."""

from __future__ import annotations

import base64
import logging

import httpx
import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.settrade.client import (
    SettradeClient,
    redact_path,
    redact_payload,
)
from src.quant_execution_engine.adapters.settrade.errors import (
    SettradeTransportError,
    SettradeVenueRejection,
)

_BASE = "https://open-api-test.settrade.com"
_BROKER = "098"
_CODE = "ABCAPP"
_APP_ID = "app-id-xyz"
_APP_SECRET = base64.b64encode((0x1234567890ABCDEF).to_bytes(32, "big")).decode()
_PIN = "987654"
_ACCOUNT = "FT0017D"


def _login_url() -> str:
    return f"{_BASE}/api/oam/v1/{_BROKER}/broker-apps/{_CODE}/login"


def _token_body() -> dict[str, object]:
    return {
        "token_type": "Bearer",
        "access_token": "atk-secret-value",
        "refresh_token": "rtk-secret-value",
        "expires_in": 1800,
    }


def _client() -> SettradeClient:
    return SettradeClient(
        base_url=_BASE,
        app_id=SecretStr(_APP_ID),
        app_secret=SecretStr(_APP_SECRET),
        app_code=_CODE,
        broker_id=_BROKER,
    )


def test_redact_payload_masks_secrets_case_insensitively() -> None:
    redacted = redact_payload(
        {
            "pin": _PIN,
            "apiKey": _APP_ID,
            "signature": "deadbeef",
            "refreshToken": "rtk",
            "accessToken": "atk",
            "token": "t",
            "password": "hunter2",
            "symbol": "PTT",
            "volume": 100,
        }
    )
    for key in ("pin", "apiKey", "signature", "refreshToken", "accessToken", "token", "password"):
        assert redacted[key] == "***"
    assert redacted["symbol"] == "PTT"
    assert redacted["volume"] == 100


def test_redact_path_masks_account_segment() -> None:
    masked = redact_path("api/seosd/v3/098/accounts/FT0017D/orders")
    assert "FT0017D" not in masked
    assert masked == "api/seosd/v3/098/accounts/***/orders"
    # Trailing account (no following segment) is masked too.
    assert redact_path("accounts/FT0017D") == "accounts/***"
    # No account segment -> unchanged.
    assert redact_path("api/oam/v1/098/broker-apps/X/login").endswith("/login")


@respx.mock
async def test_2xx_empty_body_returns_empty_dict() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.patch(f"{_BASE}/orders/1/change").respond(status_code=200, content=b"")
    client = _client()
    assert await client.patch_json("orders/1/change", {"pin": _PIN}) == {}
    await client.aclose()


@respx.mock
async def test_2xx_json_passthrough() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(json={"a": 1, "b": [2, 3]})
    client = _client()
    assert await client.get_json("x") == {"a": 1, "b": [2, 3]}
    await client.aclose()


@respx.mock
async def test_structured_400_raises_venue_rejection() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.post(f"{_BASE}/x").respond(status_code=400, json={"code": "E1099", "message": "bad band"})
    client = _client()
    with pytest.raises(SettradeVenueRejection) as exc:
        await client.post_json("x", {"pin": _PIN})
    assert exc.value.venue_code == "E1099"
    assert exc.value.status_code == 400
    assert "bad band" in str(exc.value)
    await client.aclose()


@respx.mock
async def test_500_raises_transport_error() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(status_code=503)
    client = _client()
    with pytest.raises(SettradeTransportError, match="HTTP 503"):
        await client.get_json("x")
    await client.aclose()


@respx.mock
async def test_connect_and_timeout_errors_raise_transport_error() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").mock(side_effect=httpx.ConnectError("down"))
    client = _client()
    with pytest.raises(SettradeTransportError, match="failed"):
        await client.get_json("x")
    respx.get(f"{_BASE}/y").mock(side_effect=httpx.TimeoutException("slow"))
    with pytest.raises(SettradeTransportError, match="failed"):
        await client.get_json("y")
    await client.aclose()


@respx.mock
async def test_last_wire_ok_transitions() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    client = _client()
    assert client.last_wire_ok is None
    respx.get(f"{_BASE}/ok").respond(json={})
    await client.get_json("ok")
    assert client.last_wire_ok is True
    # A structured 4xx is still a successful HTTP exchange: last_wire_ok stays True.
    respx.post(f"{_BASE}/reject").respond(status_code=400, json={"code": "E", "message": "no"})
    with pytest.raises(SettradeVenueRejection):
        await client.post_json("reject", {"pin": _PIN})
    assert client.last_wire_ok is True
    # A transport error flips it False.
    respx.get(f"{_BASE}/down").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(SettradeTransportError):
        await client.get_json("down")
    assert client.last_wire_ok is False
    await client.aclose()


@respx.mock
async def test_rate_headers_parse_into_both_buckets_with_zero_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/g").respond(
        json={},
        headers={
            "X-RateLimit-Remaining-second": "4",
            "X-RateLimit-Remaining-minute": "59",
            "X-RateLimit-Limit-second": "5",
            "X-RateLimit-Limit-minute": "60",
        },
    )
    respx.post(f"{_BASE}/w").respond(
        json={},
        headers={
            "X-RateLimit-Remaining-second": "0",
            "X-RateLimit-Remaining-minute": "10",
            "X-RateLimit-Limit-second": "5",
            "X-RateLimit-Limit-minute": "60",
        },
    )
    client = _client()
    await client.get_json("g")
    with caplog.at_level(logging.WARNING):
        await client.post_json("w", {"pin": _PIN})
    snapshot = client.rate_snapshot()
    assert snapshot["GET"].remaining_second == 4
    assert snapshot["GET"].limit_minute == 60
    assert snapshot["WRITE"].remaining_second == 0
    assert any("rate budget exhausted bucket=WRITE" in r.getMessage() for r in caplog.records)
    await client.aclose()


@respx.mock
async def test_secret_hygiene_nothing_sensitive_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Run login + a pin-bearing request at DEBUG; assert no secret reaches a record."""
    respx.post(_login_url()).respond(json=_token_body())
    respx.post(f"{_BASE}/api/seosd/v3/{_BROKER}/accounts/{_ACCOUNT}/orders").respond(
        json={"orderNo": 1614803}
    )
    client = _client()
    with caplog.at_level(logging.DEBUG):
        await client.post_json(
            f"api/seosd/v3/{_BROKER}/accounts/{_ACCOUNT}/orders",
            {"pin": _PIN, "symbol": "S50H23", "volume": 1},
        )
    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert _PIN not in rendered
    assert _APP_SECRET not in rendered
    assert "atk-secret-value" not in rendered  # access token
    assert "rtk-secret-value" not in rendered  # refresh token
    assert _ACCOUNT not in rendered  # account number redacted in the path
    assert "S50H23" in rendered  # non-secret payload IS logged at DEBUG
    await client.aclose()


@respx.mock
async def test_repr_does_not_expose_token_state() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(json={})
    client = _client()
    await client.get_json("x")
    rendered = repr(client)
    assert "atk-secret-value" not in rendered
    assert "rtk-secret-value" not in rendered
    assert _APP_SECRET not in rendered
    await client.aclose()


async def test_injected_client_is_not_closed() -> None:
    async with httpx.AsyncClient() as injected:
        client = SettradeClient(
            base_url=_BASE,
            app_id=SecretStr(_APP_ID),
            app_secret=SecretStr(_APP_SECRET),
            app_code=_CODE,
            broker_id=_BROKER,
            client=injected,
        )
        await client.aclose()
        assert not injected.is_closed


@respx.mock
async def test_2xx_non_json_body_returns_empty_dict() -> None:
    """A 2xx with a non-empty but non-JSON body degrades to {} (success-shaped)."""
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(status_code=200, content=b"<html>ok</html>")
    client = _client()
    assert await client.get_json("x") == {}
    await client.aclose()


@respx.mock
async def test_non_json_4xx_error_body_raises_transport_error() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(status_code=400, content=b"<html>nope</html>")
    client = _client()
    with pytest.raises(SettradeTransportError, match="non-JSON error"):
        await client.get_json("x")
    await client.aclose()


@respx.mock
async def test_unstructured_4xx_json_raises_transport_error() -> None:
    """A 4xx whose JSON lacks code/message is unstructured -> transport error."""
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(status_code=422, json={"unexpected": "shape"})
    client = _client()
    with pytest.raises(SettradeTransportError, match="unstructured error"):
        await client.get_json("x")
    await client.aclose()


@respx.mock
async def test_post_and_patch_convenience_wrappers() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    post = respx.post(f"{_BASE}/p").respond(json={"orderNo": 1})
    patch = respx.patch(f"{_BASE}/q/change").respond(status_code=200, content=b"")
    client = _client()
    assert await client.post_json("p", {"pin": _PIN}) == {"orderNo": 1}
    assert await client.patch_json("q/change", {"pin": _PIN}) == {}
    assert post.called and patch.called
    await client.aclose()


@respx.mock
async def test_rate_header_non_integer_is_ignored() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(
        json={}, headers={"X-RateLimit-Remaining-second": "not-a-number"}
    )
    client = _client()
    await client.get_json("x")
    assert client.rate_snapshot()["GET"].remaining_second is None
    await client.aclose()


@respx.mock
async def test_login_non_json_body_raises_auth_error() -> None:
    respx.post(_login_url()).respond(status_code=200, content=b"<html>not json</html>")
    client = _client()
    with pytest.raises(SettradeTransportError, match="non-JSON"):
        await client.get_json("x")
    await client.aclose()

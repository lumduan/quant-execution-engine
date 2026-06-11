"""OAuth lifecycle: login wire shape, ECDSA signature, refresh, 401 retry, single-flight."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr
from src.quant_execution_engine.adapters.settrade.client import SettradeClient, sign_content
from src.quant_execution_engine.adapters.settrade.errors import SettradeAuthError

_BASE = "https://open-api-test.settrade.com"
_BROKER = "098"
_CODE = "ABCAPP"
_APP_ID = "app-id-xyz"

# A deterministic EC P-256 private key whose raw scalar is base64-encoded as the
# app_secret (exactly how the SDK derives the signing key).
_PRIVATE_KEY = ec.derive_private_key(0x1234567890ABCDEF1234567890ABCDEF, ec.SECP256R1())
_RAW = (0x1234567890ABCDEF1234567890ABCDEF).to_bytes(32, "big")
_APP_SECRET = base64.b64encode(_RAW).decode()


class _Clock:
    """An injectable monotonic clock for token-expiry tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def _login_url() -> str:
    return f"{_BASE}/api/oam/v1/{_BROKER}/broker-apps/{_CODE}/login"


def _refresh_url() -> str:
    return f"{_BASE}/api/oam/v1/{_BROKER}/broker-apps/{_CODE}/refresh-token"


def _token_body(expires_in: int = 1800, suffix: str = "1") -> dict[str, object]:
    return {
        "token_type": "Bearer",
        "access_token": f"atk-{suffix}",
        "refresh_token": f"rtk-{suffix}",
        "expires_in": expires_in,
    }


def _client(now: _Clock | None = None) -> SettradeClient:
    return SettradeClient(
        base_url=_BASE,
        app_id=SecretStr(_APP_ID),
        app_secret=SecretStr(_APP_SECRET),
        app_code=_CODE,
        broker_id=_BROKER,
        now=now or _Clock(),
    )


@respx.mock
async def test_login_wire_shape_and_verified_signature() -> None:
    route = respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(json={"ok": True})
    client = _client()
    await client.get_json("x")
    assert route.called
    body = route.calls.last.request.read()
    import json

    payload = json.loads(body)
    assert payload["apiKey"] == _APP_ID
    assert payload["params"] == ""
    assert payload["timestamp"].isdigit()
    assert all(c in "0123456789abcdef" for c in payload["signature"])  # hex
    # VERIFY the signature with the public key derived from the same secret.
    content = f"{_APP_ID}.{''}.{payload['timestamp']}"
    _PRIVATE_KEY.public_key().verify(
        bytes.fromhex(payload["signature"]), content.encode(), ec.ECDSA(hashes.SHA256())
    )
    await client.aclose()


def test_sign_content_is_verifiable() -> None:
    sig = sign_content(SecretStr(_APP_SECRET), "hello.world")
    _PRIVATE_KEY.public_key().verify(bytes.fromhex(sig), b"hello.world", ec.ECDSA(hashes.SHA256()))


@respx.mock
async def test_proactive_refresh_inside_margin() -> None:
    clock = _Clock()
    login = respx.post(_login_url()).respond(json=_token_body(expires_in=1800, suffix="1"))
    refresh = respx.post(_refresh_url()).respond(json=_token_body(expires_in=1800, suffix="2"))
    data = respx.get(f"{_BASE}/x").respond(json={"ok": True})
    client = _client(now=clock)
    await client.get_json("x")  # login
    assert login.call_count == 1
    # Advance to inside the 100s refresh margin (expires at +1800).
    clock.value += 1800 - 50
    await client.get_json("x")
    assert refresh.call_count == 1
    assert login.call_count == 1
    assert data.call_count == 2
    body = refresh.calls.last.request.read()
    import json

    payload = json.loads(body)
    assert payload == {"apiKey": _APP_ID, "refreshToken": "rtk-1"}
    await client.aclose()


@respx.mock
async def test_refresh_failure_falls_back_to_login() -> None:
    clock = _Clock()
    login = respx.post(_login_url()).respond(json=_token_body(expires_in=1800, suffix="1"))
    respx.post(_refresh_url()).respond(status_code=401, json={"code": "X", "message": "revoked"})
    respx.get(f"{_BASE}/x").respond(json={"ok": True})
    client = _client(now=clock)
    await client.get_json("x")  # login #1
    clock.value += 1800 - 50  # inside margin -> refresh attempted, fails -> login
    await client.get_json("x")
    assert login.call_count == 2  # fell back to a fresh login
    await client.aclose()


@respx.mock
async def test_reactive_401_triggers_one_reauth_and_retry() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.post(_refresh_url()).respond(json=_token_body(suffix="2"))
    # First data call 401s; the retry (after re-auth) succeeds.
    respx.get(f"{_BASE}/x").mock(
        side_effect=[
            httpx.Response(401, json={"code": "401", "message": "expired"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = _client()
    result = await client.get_json("x")
    assert result == {"ok": True}
    await client.aclose()


@respx.mock
async def test_second_401_raises_auth_error() -> None:
    respx.post(_login_url()).respond(json=_token_body())
    respx.post(_refresh_url()).respond(json=_token_body(suffix="2"))
    respx.get(f"{_BASE}/x").respond(status_code=401, json={"code": "401", "message": "expired"})
    client = _client()
    with pytest.raises(SettradeAuthError, match="401 after re-auth"):
        await client.get_json("x")
    await client.aclose()


@respx.mock
async def test_login_failure_raises_auth_error_without_creds() -> None:
    respx.post(_login_url()).respond(
        status_code=403, json={"code": "AUTH01", "message": "bad signature"}
    )
    client = _client()
    with pytest.raises(SettradeAuthError) as exc:
        await client.get_json("x")
    rendered = str(exc.value)
    assert "AUTH01" in rendered
    assert _APP_SECRET not in rendered  # the secret never leaks into the message
    await client.aclose()


@respx.mock
async def test_single_flight_n_concurrent_callers_login_once() -> None:
    login = respx.post(_login_url()).respond(json=_token_body())
    respx.get(f"{_BASE}/x").respond(json={"ok": True})
    client = _client()
    await asyncio.gather(*(client.get_json("x") for _ in range(5)))
    assert login.call_count == 1  # single-flight: exactly one login
    await client.aclose()

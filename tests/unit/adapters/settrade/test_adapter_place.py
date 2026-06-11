"""SettradeAdapter.place: ack parsing, cache, never-swallowed venue rejects."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
from src.quant_execution_engine.adapters.settrade.client import SettradeClient
from src.quant_execution_engine.adapters.settrade.errors import SettradeTransportError
from src.quant_execution_engine.contracts.enums import Broker, Market

from tests.conftest import make_order

_BASE = "https://open-api-test.settrade.com"
_BROKER = "098"
_CODE = "ABCAPP"
_APP_ID = "app-id-xyz"
_APP_SECRET = base64.b64encode((0x1234567890ABCDEF).to_bytes(32, "big")).decode()
_PIN = "987654"
_ACCOUNT = "ACC-TEST"
_TOKEN = {
    "token_type": "Bearer",
    "access_token": "atk",
    "refresh_token": "rtk",
    "expires_in": 1800,
}
# Phase 4.1 dual-adapter: distinct apps so respx distinguishes the two logins and
# the per-app token body lets assertions read the Authorization header.
_EQUITY_CODE = "ALGOEQ"
_DERIV_CODE = "ALGODRV"
_EQUITY_APP_ID = "equity-app"
_DERIV_APP_ID = "deriv-app"
_EQUITY_TOKEN = {**_TOKEN, "access_token": "equity-atk"}
_DERIV_TOKEN = {**_TOKEN, "access_token": "deriv-atk"}


def _login_url(app_code: str = _CODE) -> str:
    return f"{_BASE}/api/oam/v1/{_BROKER}/broker-apps/{app_code}/login"


def _set_orders_url(account: str = _ACCOUNT) -> str:
    return f"{_BASE}/api/seos/v3/{_BROKER}/accounts/{account}/orders"


def _tfex_orders_url(account: str = _ACCOUNT) -> str:
    return f"{_BASE}/api/seosd/v3/{_BROKER}/accounts/{account}/orders"


def make_client(app_code: str = _CODE, app_id: str = _APP_ID) -> SettradeClient:
    return SettradeClient(
        base_url=_BASE,
        app_id=SecretStr(app_id),
        app_secret=SecretStr(_APP_SECRET),
        app_code=app_code,
        broker_id=_BROKER,
    )


def make_adapter(**kwargs: Any) -> SettradeAdapter:
    """Sandbox shape: ONE client serves BOTH markets (single-app InnovestX/098)."""
    client = make_client()
    return SettradeAdapter(
        clients={Market.SET: client, Market.TFEX: client},
        broker_id=_BROKER,
        pin=SecretStr(_PIN),
        **kwargs,
    )


def make_dual_adapter(**kwargs: Any) -> SettradeAdapter:
    """Per-market shape: a distinct equity (SET) + derivatives (TFEX) client."""
    return SettradeAdapter(
        clients={
            Market.SET: make_client(app_code=_EQUITY_CODE, app_id=_EQUITY_APP_ID),
            Market.TFEX: make_client(app_code=_DERIV_CODE, app_id=_DERIV_APP_ID),
        },
        broker_id=_BROKER,
        pin=SecretStr(_PIN),
        **kwargs,
    )


def make_set_only_adapter(**kwargs: Any) -> SettradeAdapter:
    """SET configured, TFEX unconfigured (a forgotten/partial derivatives trio)."""
    return SettradeAdapter(
        clients={Market.SET: make_client(app_code=_EQUITY_CODE, app_id=_EQUITY_APP_ID)},
        broker_id=_BROKER,
        pin=SecretStr(_PIN),
        **kwargs,
    )


def _login_route() -> None:
    respx.post(_login_url()).respond(json=_TOKEN)


def _dual_login_routes() -> None:
    """Per-app login routes with distinct token bodies (read the Authorization)."""
    respx.post(_login_url(_EQUITY_CODE)).respond(json=_EQUITY_TOKEN)
    respx.post(_login_url(_DERIV_CODE)).respond(json=_DERIV_TOKEN)


def _settrade_order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "settrade", "price": "100.00"}
    payload.update(overrides)
    return make_order(**payload)


@respx.mock
async def test_place_set_acks_and_caches_ref() -> None:
    _login_route()
    respx.post(_set_orders_url()).respond(json={"orderNo": "SET-7001"})
    cancel_route = respx.patch(
        f"{_BASE}/api/seos/v3/{_BROKER}/accounts/{_ACCOUNT}/orders/SET-7001/cancel"
    ).respond(json={})
    adapter = make_adapter()
    order = _settrade_order()
    ack = await adapter.place(order)
    assert not ack.rejected
    assert ack.broker_order_id == "SET-7001"
    assert ack.fills == ()  # fills arrive via reconciliation in v1
    # Cache populated: a cancel with NO resolver resolves from the warm cache.
    cancel_ack = await adapter.cancel(order.client_order_id)
    assert cancel_ack.ok
    assert cancel_route.called
    await adapter.aclose()


@respx.mock
async def test_place_venue_4xx_rejection_carries_code_and_message() -> None:
    _login_route()
    respx.post(_set_orders_url()).respond(
        status_code=400, json={"code": "1101", "message": "invalid price band"}
    )
    adapter = make_adapter()
    ack = await adapter.place(_settrade_order())
    assert ack.rejected
    assert ack.reject_reason is not None
    assert "1101" in ack.reject_reason and "invalid price band" in ack.reject_reason
    await adapter.aclose()


@respx.mock
async def test_place_2xx_rejected_order_object_is_rejected_ack() -> None:
    """The venue can return a rejected order object with a 2xx status."""
    _login_route()
    respx.post(_set_orders_url()).respond(
        json={
            "orderNo": "SET-7009",
            "rejectCode": "55",
            "rejectReason": "circuit breaker halt",
        }
    )
    adapter = make_adapter()
    ack = await adapter.place(_settrade_order())
    assert ack.rejected
    assert ack.reject_reason is not None and "circuit breaker halt" in ack.reject_reason
    await adapter.aclose()


@respx.mock
async def test_place_transport_failure_propagates_as_lost_ack_window() -> None:
    _login_route()
    respx.post(_set_orders_url()).respond(status_code=503)
    adapter = make_adapter()
    with pytest.raises(SettradeTransportError):
        await adapter.place(_settrade_order())
    await adapter.aclose()


@respx.mock
async def test_place_missing_order_no_raises_adapter_error() -> None:
    _login_route()
    respx.post(_set_orders_url()).respond(json={"message": "ok"})
    adapter = make_adapter()
    with pytest.raises(AdapterError, match="orderNo"):
        await adapter.place(_settrade_order())
    await adapter.aclose()


@respx.mock
async def test_place_mapping_error_rejects_preflight_without_http() -> None:
    _login_route()
    route = respx.post(_set_orders_url()).respond(json={"orderNo": "X"})
    adapter = make_adapter()
    # A non-tick price cannot survive the exact wire-float round-trip — a
    # SettradeMappingError raised before any HTTP I/O, surfaced as a rejected ack.
    ack = await adapter.place(_settrade_order(price="0.1234567890123456789"))
    assert ack.rejected
    assert ack.reject_reason is not None and "mapping" in ack.reject_reason
    assert not route.called  # rejected before any venue I/O
    await adapter.aclose()


@respx.mock
async def test_place_tfex_routes_to_derivatives_book() -> None:
    _login_route()
    route = respx.post(_tfex_orders_url()).respond(json={"orderNo": 9001})
    adapter = make_adapter()
    order = _settrade_order(market="TFEX", symbol="S50H26", position_effect="OPEN", price="950.0")
    ack = await adapter.place(order)
    assert ack.broker_order_id == "9001"  # int orderNo normalized to str
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["side"] == "Long" and sent["position"] == "Open"
    await adapter.aclose()


def test_capabilities_are_exactly_the_settrade_matrix_rows() -> None:
    adapter = make_adapter()
    rows = adapter.capabilities()
    assert {row.market.value for row in rows} == {"SET", "TFEX"}
    assert all(row.broker is Broker.SETTRADE for row in rows)
    assert all(row.amend == "native" for row in rows)


@respx.mock
async def test_place_pin_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    _login_route()
    respx.post(_set_orders_url()).respond(json={"orderNo": "SET-1"})
    adapter = make_adapter()
    order = _settrade_order()
    with caplog.at_level(logging.DEBUG):
        await adapter.place(order)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert _PIN not in rendered
    assert order.account not in rendered
    await adapter.aclose()


def test_make_adapter_with_injected_httpx_client() -> None:
    """Smoke: the adapter accepts a client built on an injected transport."""
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={}))
    client = SettradeClient(
        base_url=_BASE,
        app_id=SecretStr(_APP_ID),
        app_secret=SecretStr(_APP_SECRET),
        app_code=_CODE,
        broker_id=_BROKER,
        client=httpx.AsyncClient(transport=transport),
    )
    adapter = SettradeAdapter(
        clients={Market.SET: client, Market.TFEX: client},
        broker_id=_BROKER,
        pin=SecretStr(_PIN),
    )
    assert adapter.broker is Broker.SETTRADE


def test_empty_clients_map_is_rejected() -> None:
    """The ctor guards against a market-less adapter (would route nothing)."""
    with pytest.raises(ValueError, match="at least one configured market"):
        SettradeAdapter(clients={}, broker_id=_BROKER, pin=SecretStr(_PIN))


# --------------------------------------------------- Phase 4.1 per-market place


@respx.mock
async def test_place_set_routes_equity_client_only() -> None:
    """SET place hits the equity login + order; the derivatives app is untouched."""
    _dual_login_routes()
    equity_login = respx.post(_login_url(_EQUITY_CODE)).respond(json=_EQUITY_TOKEN)
    deriv_login = respx.post(_login_url(_DERIV_CODE)).respond(json=_DERIV_TOKEN)
    order_route = respx.post(_set_orders_url()).respond(json={"orderNo": "SET-7001"})
    adapter = make_dual_adapter()
    ack = await adapter.place(_settrade_order())
    assert ack.broker_order_id == "SET-7001"
    assert equity_login.called and not deriv_login.called
    # The order call carried the EQUITY token, never the derivatives one.
    auth = order_route.calls.last.request.headers["Authorization"]
    assert auth == "Bearer equity-atk"
    await adapter.aclose()


@respx.mock
async def test_place_tfex_routes_derivatives_client_only() -> None:
    """TFEX place mirrors SET on the derivatives app (the other login untouched)."""
    _dual_login_routes()
    equity_login = respx.post(_login_url(_EQUITY_CODE)).respond(json=_EQUITY_TOKEN)
    deriv_login = respx.post(_login_url(_DERIV_CODE)).respond(json=_DERIV_TOKEN)
    order_route = respx.post(_tfex_orders_url()).respond(json={"orderNo": 9001})
    adapter = make_dual_adapter()
    order = _settrade_order(market="TFEX", symbol="S50H26", position_effect="OPEN", price="950.0")
    ack = await adapter.place(order)
    assert ack.broker_order_id == "9001"
    assert deriv_login.called and not equity_login.called
    auth = order_route.calls.last.request.headers["Authorization"]
    assert auth == "Bearer deriv-atk"
    await adapter.aclose()


@respx.mock
async def test_place_unconfigured_market_rejects_with_zero_http() -> None:
    """A SET-only adapter rejects a TFEX order with the no-app reason — no I/O."""
    login = respx.post(_login_url(_EQUITY_CODE)).respond(json=_EQUITY_TOKEN)
    tfex = respx.post(_tfex_orders_url())
    adapter = make_set_only_adapter()
    order = _settrade_order(market="TFEX", symbol="S50H26", position_effect="OPEN", price="950.0")
    ack = await adapter.place(order)
    assert ack.rejected
    assert ack.reject_reason == "settrade: no TFEX broker app configured"
    assert not login.called and not tfex.called  # zero HTTP recorded
    await adapter.aclose()


@respx.mock
async def test_sandbox_place_both_markets_records_one_login() -> None:
    """The sandbox single client logs in exactly ONCE across a SET + TFEX place."""
    login = respx.post(_login_url()).respond(json=_TOKEN)
    respx.post(_set_orders_url()).respond(json={"orderNo": "SET-1"})
    respx.post(_tfex_orders_url()).respond(json={"orderNo": 9001})
    adapter = make_adapter()
    await adapter.place(_settrade_order())
    await adapter.place(
        _settrade_order(market="TFEX", symbol="S50H26", position_effect="OPEN", price="950.0")
    )
    assert login.call_count == 1  # one shared session, one login
    await adapter.aclose()

"""GET /accounts/{account}/order-path — the no-order probe of the venue's ORDER path (GH #468).

On 2026-11-24 the AWS Liberator bridge refused every TFEX place with HTTP 403 while
``session/status`` and every read stayed healthy. The probe answers that question without placing
an order: it calls the bridge's fee calculation (``order/pre-place/{set|tfex}``), which takes the
same body as a place and places nothing.

The bridge is respx-mocked; the route is driven through the real production path
(``OrderRouter._resolve_adapter`` -> stage ladder -> EH6), at ``micro_live``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from src.quant_execution_engine.adapters.base import OrderPathOutcome
from src.quant_execution_engine.adapters.liberator import mapping
from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
from src.quant_execution_engine.api.main import create_app
from src.quant_execution_engine.contracts.enums import (
    Broker,
    Market,
    OrderType,
    PositionEffect,
    Side,
    Tif,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import make_settings
from tests.unit.adapters.liberator.test_adapter_place import _BASE, make_adapter

_TFEX = "70000007"
_SET = "70000002"
_PRE_TFEX = f"{_BASE}/order/pre-place/tfex"
_PRE_SET = f"{_BASE}/order/pre-place/set"
_READ_KEY = "read-key-for-order-path-tests"


def _ok() -> dict[str, Any]:
    return {"success": True, "message": "calculated", "data": {"errorCode": 0, "result": {}}}


def _order(market: Market = Market.TFEX, price: str = "700") -> NormalizedOrder:
    return NormalizedOrder(
        client_order_id=str(uuid4()),
        broker=Broker.LIBERATOR,
        account=_TFEX if market is Market.TFEX else _SET,
        market=market,
        symbol="S50Z26" if market is Market.TFEX else "PTT",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        price=Decimal(price),
        quantity=1 if market is Market.TFEX else 100,
        tif=Tif.DAY,
        position_effect=PositionEffect.OPEN if market is Market.TFEX else None,
    )


def _no_place_route() -> respx.Route:
    """Any call to a REAL place path. Every test asserts it was never hit."""
    return respx.post(url__regex=rf"{_BASE}/order/place/.*").respond(json=_ok())


# ---- the adapter: what each bridge answer means --------------------------------------------------


@respx.mock
async def test_ok_calls_pre_place_with_the_exact_place_body_and_never_places() -> None:
    place = _no_place_route()
    pre = respx.post(_PRE_TFEX).respond(json=_ok())
    order = _order()
    probe = await make_adapter().probe_order_path(order)
    assert probe.outcome is OrderPathOutcome.OK and probe.ok and probe.bridge_status == 200
    assert pre.call_count == 1 and place.call_count == 0
    import json

    assert json.loads(pre.calls[0].request.content) == mapping.to_place_payload(order)


@respx.mock
async def test_set_goes_to_the_set_pre_place() -> None:
    place = _no_place_route()
    pre = respx.post(_PRE_SET).respond(json=_ok())
    probe = await make_adapter().probe_order_path(_order(Market.SET, "30"))
    assert probe.ok and pre.call_count == 1 and place.call_count == 0


# The bridge's own bodies, read from liberator-trading-api at 3b7632d (both markets share them).
_AUTH_401 = {  # before 3b7632d this was ALSO the refused-session answer (AWS, 2026-11-24/25)
    "success": False,
    "error": "authentication_error",
    "message": "Authentication failed: HTTP 403. Please check auth token validity",
    "details": {"auth_error": "No valid auth token or PIN available"},
}
# 🔶 TEMPORARY prose pin: at 3b7632d the endpoint's NetworkError arm drops the exception's
# details, so this sentence is the only marker. liberator-trading-api PR #65 carries
# `details.classification` through; `_EDGE_BLOCK_503_FUTURE` is that shape. Keep the prose case
# until #65 is RUNNING on every node (a merge deploys nothing).
_REFUSED_TEXT = (
    "TFEX order pre-place API request failed: This HTTP SESSION was refused: HTTP 403 with an "
    "empty body. The stored auth token is not necessarily wrong - a session can read fine and "
    "still be unable to send orders."
)
_EDGE_BLOCK_503 = {
    "success": False,
    "error": "network_error",
    "message": _REFUSED_TEXT,
    "details": {"network_error": _REFUSED_TEXT},
}
_EDGE_BLOCK_503_FUTURE = {
    "success": False,
    "error": "network_error",
    "message": "TFEX order pre-place API request failed: reworded",
    "details": {"network_error": "reworded", "classification": "edge_block"},
}
_OUTAGE_503 = {
    "success": False,
    "error": "network_error",
    "message": "TFEX order pre-place API request failed: connection reset by peer",
    "details": {"network_error": "connection reset by peer"},
}


@pytest.mark.parametrize(
    ("status", "body"),
    [(401, _AUTH_401), (403, _AUTH_401), (503, _EDGE_BLOCK_503), (503, _EDGE_BLOCK_503_FUTURE)],
    ids=["401-before-3b7632d", "403", "503-edge-block-prose", "503-edge-block-details"],
)
@respx.mock
async def test_the_468_signature_is_refused_before_and_after_the_bridge_fix(
    status: int, body: dict[str, Any]
) -> None:
    respx.post(_PRE_TFEX).respond(status_code=status, json=body)
    probe = await make_adapter().probe_order_path(_order())
    assert probe.outcome is OrderPathOutcome.REFUSED
    assert probe.bridge_status == status and not probe.ok


@respx.mock
async def test_a_refused_probe_never_feeds_the_breaker() -> None:
    """A breaker trip mass-cancels: a probe must report, never trip."""
    adapter = make_adapter()
    respx.post(_PRE_TFEX).respond(status_code=503, json=_EDGE_BLOCK_503)
    for _ in range(5):
        assert (await adapter.probe_order_path(_order())).outcome is OrderPathOutcome.REFUSED
    assert adapter.breaker._consecutive_failures == 0
    assert adapter.breaker.state.value == "closed"


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (503, _OUTAGE_503, OrderPathOutcome.UNREACHABLE),
        (500, {"success": False, "error": "internal_error"}, OrderPathOutcome.UNREACHABLE),
        (422, {"detail": [{"msg": "field required"}]}, OrderPathOutcome.INPUT_REJECTED),
        (
            400,
            {"success": False, "error": "validation_error", "message": "validation failed"},
            OrderPathOutcome.INPUT_REJECTED,
        ),
        (
            400,
            {
                "success": False,
                "message": "Order pre-place calculation failed: no such series",
                "error_code": "5",
                "data": {"errorCode": 5, "errTitle": "x", "errMsg": "no such series"},
            },
            OrderPathOutcome.VENUE_REJECTED,
        ),
        (
            400,
            {"success": False, "message": "Invalid JSON", "error_code": "json_parse_error"},
            OrderPathOutcome.UNEXPECTED,
        ),
        (
            400,
            {
                "success": False,
                "message": "Invalid response structure from API: 1 validation error",
                "error_code": "validation_error",
            },
            OrderPathOutcome.UNEXPECTED,
        ),
        (404, {"detail": "Not Found"}, OrderPathOutcome.UNEXPECTED),
        (
            200,
            {"success": True, "data": {"errorCode": 7, "errTitle": "", "errMsg": "closed"}},
            OrderPathOutcome.VENUE_REJECTED,
        ),
    ],
    ids=[
        "503-outage",
        "500",
        "422",
        "400-validation",
        "400-venue-declined",
        "400-json-parse",
        "400-venue-answer-unreadable",
        "404",
        "200-nonzero-code",
    ],
)
@respx.mock
async def test_each_other_answer_has_its_own_outcome(
    status: int, body: dict[str, Any], outcome: OrderPathOutcome
) -> None:
    respx.post(_PRE_TFEX).respond(status_code=status, json=body)
    probe = await make_adapter().probe_order_path(_order())
    assert probe.outcome is outcome and not probe.ok


@respx.mock
async def test_a_non_envelope_body_is_unexpected_not_ok() -> None:
    respx.post(_PRE_TFEX).respond(status_code=200, text="<html>blocked</html>")
    probe = await make_adapter().probe_order_path(_order())
    assert probe.outcome is OrderPathOutcome.UNEXPECTED and probe.bridge_status == 200


@respx.mock
async def test_no_connection_is_unreachable_and_does_not_raise() -> None:
    respx.post(_PRE_TFEX).mock(side_effect=httpx.ConnectError("refused"))
    probe = await make_adapter().probe_order_path(_order())
    assert probe.outcome is OrderPathOutcome.UNREACHABLE and probe.bridge_status is None


@respx.mock
async def test_a_set_price_the_wire_cannot_carry_is_input_rejected_before_any_call() -> None:
    pre = respx.post(_PRE_SET).respond(json=_ok())
    probe = await make_adapter().probe_order_path(_order(Market.SET, "30.125"))
    assert probe.outcome is OrderPathOutcome.INPUT_REJECTED and pre.call_count == 0


@respx.mock
async def test_long_digit_runs_are_masked_in_the_message() -> None:
    respx.post(_PRE_TFEX).respond(
        status_code=400,
        json={"success": False, "data": {"errorCode": 3, "errMsg": f"account {_TFEX} blocked"}},
    )
    probe = await make_adapter().probe_order_path(_order())
    assert _TFEX not in (probe.message or "") and "****" in (probe.message or "")


@respx.mock
async def test_streaming_pro_is_unsupported_and_makes_no_call() -> None:
    """The SP bridge has place/change/cancel only: no call on its order path places nothing."""
    from tests.unit.adapters.streaming_pro.test_adapter_place import make_adapter as sp_adapter

    anything = respx.route().respond(json=_ok())
    probe = await sp_adapter().probe_order_path(_order())
    assert probe.outcome is OrderPathOutcome.UNSUPPORTED and not probe.ok
    assert "StreamingProAdapter" in (probe.message or "")
    assert anything.call_count == 0


# ---- the route, through the real production path ---------------------------------------------


def _client(
    monkeypatch: pytest.MonkeyPatch, adapter: LiberatorAdapter | None = None, **overrides: Any
) -> TestClient:
    from src.quant_execution_engine.api import deps
    from src.quant_execution_engine.core.router import OrderRouter

    store = MemStore()
    patch_repositories(monkeypatch, store)
    base: dict[str, Any] = {
        "public_mode": False,
        "stage": "micro_live",
        "real_routing_accounts": [_SET, _TFEX],
        "read_api_key": _READ_KEY,
    }
    settings = make_settings(**{**base, **overrides})
    order_router = OrderRouter(
        settings=settings,
        pool=object(),
        redis=FakeRedis(),
        liberator_adapter=adapter or make_adapter(),
    )
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_pool_dep] = lambda: object()
    app.dependency_overrides[deps.get_redis_dep] = lambda: FakeRedis()
    app.dependency_overrides[deps.get_router_dep] = lambda: order_router
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    return TestClient(app, headers=headers)


_Q = "broker=liberator&market=TFEX&symbol=S50Z26&price=700"


@respx.mock
def test_route_reports_ok_and_never_places(monkeypatch: pytest.MonkeyPatch) -> None:
    place = _no_place_route()
    respx.post(_PRE_TFEX).respond(json=_ok())
    r = _client(monkeypatch).get(f"/accounts/{_TFEX}/order-path?{_Q}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["outcome"] == "ok" and body["places_no_order"] is True
    assert body["cached"] is False and body["bridge_status"] == 200
    assert _TFEX not in r.text  # the account is in the URL; the answer does not echo it
    assert place.call_count == 0


@respx.mock
def test_route_reports_refused_as_200_with_the_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(_PRE_TFEX).respond(status_code=401, json=_AUTH_401)
    r = _client(monkeypatch).get(f"/accounts/{_TFEX}/order-path?{_Q}")
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["outcome"] == "refused"


@respx.mock
def test_one_venue_call_per_window_then_a_fresh_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.quant_execution_engine.core.router as router_mod

    clock = [1000.0]
    monkeypatch.setattr(router_mod, "_monotonic", lambda: clock[0])
    pre = respx.post(_PRE_TFEX).respond(json=_ok())
    c = _client(monkeypatch)
    first = c.get(f"/accounts/{_TFEX}/order-path?{_Q}").json()
    clock[0] += 29.0
    second = c.get(f"/accounts/{_TFEX}/order-path?{_Q.replace('700', '701')}").json()
    assert pre.call_count == 1, "a repeat inside the window must not reach the venue"
    assert second["cached"] is True and second["checked_at"] == first["checked_at"]
    clock[0] += 2.0
    third = c.get(f"/accounts/{_TFEX}/order-path?{_Q}").json()
    assert pre.call_count == 2 and third["cached"] is False


@respx.mock
def test_the_read_key_may_call_it(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.post(_PRE_TFEX).respond(json=_ok())
    c = _client(monkeypatch)
    r = c.get(f"/accounts/{_TFEX}/order-path?{_Q}", headers={"X-API-Key": _READ_KEY})
    assert r.status_code == 200 and r.json()["ok"] is True


@respx.mock
def test_eh6_refuses_an_undeclared_account_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    pre = respx.post(url__regex=rf"{_BASE}/order/pre-place/.*").respond(json=_ok())
    r = _client(monkeypatch).get(f"/accounts/70000012/order-path?{_Q}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "real_routing_not_authorized"
    assert pre.call_count == 0


def test_paper_with_no_runtime_is_stage_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.quant_execution_engine.api import deps
    from src.quant_execution_engine.core.router import OrderRouter

    patch_repositories(monkeypatch, MemStore())
    settings = make_settings(public_mode=False, stage="paper")
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_router_dep] = lambda: OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis()
    )
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    r = TestClient(app, headers=headers).get(f"/accounts/{_TFEX}/order-path?{_Q}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "stage_rejected"


@pytest.mark.parametrize(
    "query",
    [
        "broker=liberator&market=TFEX&symbol=s50z26&price=700",  # lower case
        "broker=liberator&market=TFEX&symbol=S50Z26&price=0",  # not > 0
        "broker=liberator&market=TFEX&symbol=S50Z26",  # no price
        "broker=liberator&market=NYSE&symbol=S50Z26&price=700",  # not a market
    ],
)
def test_bad_inputs_are_422_before_any_call(monkeypatch: pytest.MonkeyPatch, query: str) -> None:
    with respx.mock:
        pre = respx.post(url__regex=rf"{_BASE}/order/pre-place/.*").respond(json=_ok())
        r = _client(monkeypatch).get(f"/accounts/{_TFEX}/order-path?{query}")
        assert r.status_code == 422, r.text
        assert pre.call_count == 0


async def test_an_order_the_contract_refuses_is_input_rejected_with_no_call_and_no_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreachable through the route (its query validation blocks it); a direct caller still
    gets a finding rather than a 500, and nothing reaches the venue."""
    from src.quant_execution_engine.core.router import OrderRouter

    settings = make_settings(public_mode=False, stage="micro_live", real_routing_accounts=[_TFEX])
    order_router = OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis(), liberator_adapter=make_adapter()
    )
    with respx.mock:
        anything = respx.route().respond(json=_ok())
        result = await order_router.probe_order_path(
            Broker.LIBERATOR, _TFEX, Market.TFEX, "S50Z26", Decimal("-1")
        )
        assert result.probe.outcome is OrderPathOutcome.INPUT_REJECTED and not result.cached
        assert anything.call_count == 0
    assert order_router._probe_last == {}

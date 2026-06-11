"""Router + SettradeAdapter end-to-end over MemStore with respx-mocked venue.

Mirrors the Liberator breaker e2e (``test_core_router_liberator.py``) for the
second broker: breaker trip → ``broker_circuit_open`` + mass-cancel attempted +
``/health`` surfaces ``brokers.settrade.breaker_state == "open"``; paper-stage
place intercepted to sim (zero settrade HTTP); live-stage typed reject.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx
from src.quant_execution_engine.adapters.errors import CircuitOpenError
from src.quant_execution_engine.adapters.settrade.adapter import (
    SettradeAdapter,
    SettradeOrderIdResolver,
)
from src.quant_execution_engine.adapters.settrade.heartbeat import heartbeat_pass
from src.quant_execution_engine.adapters.settrade.runtime import get_settrade_adapter
from src.quant_execution_engine.contracts.enums import Market, OrderState
from src.quant_execution_engine.contracts.errors import StageRejected
from src.quant_execution_engine.core.router import OrderRouter

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import build_client, make_order, make_settings
from tests.unit.adapters.settrade.test_adapter_place import (
    _ACCOUNT,
    _BASE,
    _BROKER,
    _login_route,
    make_adapter,
)


def _router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: MemStore | None = None,
    adapter: SettradeAdapter | None = None,
    **settings_overrides: Any,
) -> tuple[OrderRouter, MemStore]:
    store = store or MemStore()
    patch_repositories(monkeypatch, store)
    settings = make_settings(submit_lock_wait_ms=120, **settings_overrides)
    router = OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis(), settrade_adapter=adapter
    )
    return router, store


def _order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "settrade", "price": "100.00", "account": _ACCOUNT}
    payload.update(overrides)
    return make_order(**payload)


def _resolver(order_no: str, market: Market) -> SettradeOrderIdResolver:
    async def resolve(client_order_id: str) -> tuple[str, Market, str] | None:
        return (order_no, market, _ACCOUNT)

    return resolve


def _set_orders_url() -> str:
    return f"{_BASE}/api/seos/v3/{_BROKER}/accounts/{_ACCOUNT}/orders"


@respx.mock
async def test_micro_live_routes_real_and_idempotent_resubmit_one_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _login_route()
    route = respx.post(_set_orders_url()).respond(json={"orderNo": "SET-7001"})
    router, store = _router(monkeypatch, adapter=make_adapter(), stage="micro_live")
    order = _order()
    first = await router.submit(order)
    assert not first.duplicate
    assert first.result.broker_order_id == "SET-7001"
    assert first.result.engine_state is OrderState.NEW  # fills arrive via reconcile
    second = await router.submit(order)
    assert second.duplicate
    assert route.call_count == 1
    assert len(store.orders) == 1


@respx.mock
async def test_paper_intercept_zero_venue_http(monkeypatch: pytest.MonkeyPatch) -> None:
    _login_route()
    route = respx.post(_set_orders_url()).respond(json={"orderNo": "SET-X"})
    router, _ = _router(monkeypatch, adapter=make_adapter(), stage="paper")
    outcome = await router.submit(_order())
    assert outcome.result.broker_order_id is not None
    assert outcome.result.broker_order_id.startswith("SIM-")  # sim ack, real session idle
    assert not route.called  # ZERO settrade HTTP calls in paper


async def test_live_stage_is_typed_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    router, store = _router(monkeypatch, adapter=make_adapter(), stage="live")
    with pytest.raises(StageRejected, match="live"):
        await router.submit(_order())
    assert store.orders == {}  # rejected before any durable insert


async def test_micro_live_without_runtime_is_stage_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, adapter=None, stage="micro_live")
    with pytest.raises(StageRejected):
        await router.submit(_order())
    assert store.orders == {}


@respx.mock
async def test_breaker_trip_typed_error_and_mass_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter(breaker_threshold=2, resolve_order=_resolver("B-SEED", Market.SET))
    router, store = _router(monkeypatch, adapter=adapter, stage="micro_live")
    resting = _order()
    store.seed(resting, OrderState.NEW)  # open at the venue when the session dies
    _login_route()  # token acquirable: the mass-cancel can still reach the venue
    respx.patch(f"{_BASE}/api/seos/v3/{_BROKER}/accounts/{_ACCOUNT}/orders/B-SEED/cancel").respond(
        json={}
    )

    swept: list[tuple[list[str], list[str]]] = []

    async def on_trip() -> None:
        swept.append(await router.mass_cancel())

    # Prime the token so ensure_token() reuses the cache (no wire call) — then a
    # degraded session shows as last_wire_ok=False even with a valid token (the
    # Design Decision 6 blind spot the heartbeat catches).
    await adapter._client.ensure_token()  # noqa: SLF001 - test hook
    adapter._client.last_wire_ok = False  # noqa: SLF001 - test hook
    await heartbeat_pass(adapter, on_trip=on_trip)
    adapter._client.last_wire_ok = False  # noqa: SLF001 - test hook
    await heartbeat_pass(adapter, on_trip=on_trip)

    assert swept and swept[0][0] == [resting.client_order_id]
    assert store.orders[resting.client_order_id]["status"] is OrderState.CANCELLED
    with pytest.raises(CircuitOpenError) as exc_info:
        await router.submit(_order())
    assert exc_info.value.code == "broker_circuit_open"


def test_health_and_capabilities_surface_settrade_breaker_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.quant_execution_engine.adapters.settrade import runtime

    settings = make_settings()
    client, _ = build_client(settings=settings)
    body = client.get("/health").json()
    assert body["brokers"] is None  # broker-free default

    monkeypatch.setattr(runtime, "_adapter", make_adapter())
    assert get_settrade_adapter() is not None
    body = client.get("/health").json()
    assert body["brokers"]["settrade"]["breaker_state"] == "closed"
    assert body["brokers"]["settrade"]["session_healthy"] is None
    caps = client.get("/capabilities").json()
    assert caps["brokers"]["settrade"]["breaker_state"] == "closed"
    settrade_rows = [c for c in caps["capabilities"] if c["broker"] == "settrade"]
    assert settrade_rows and all(c["adapter_installed"] for c in settrade_rows)
    assert all(c["amend"] == "native" for c in settrade_rows)

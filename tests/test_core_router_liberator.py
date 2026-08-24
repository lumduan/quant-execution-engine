"""Router + LiberatorAdapter end-to-end over MemStore with respx-mocked venue.

Covers the Phase 3 required cases at the orchestration level: idempotency with
exactly one venue HTTP call, paper-stage intercept (zero HTTP), breaker trip →
``broker_circuit_open`` + mass-cancel attempted, and amend ordering (cancel
strictly before the replacement place; old CANCELLED, new full lifecycle).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import respx
from src.quant_execution_engine.adapters.errors import CircuitOpenError
from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
from src.quant_execution_engine.adapters.liberator.heartbeat import heartbeat_pass
from src.quant_execution_engine.adapters.liberator.runtime import (
    get_liberator_adapter,
)
from src.quant_execution_engine.contracts.enums import Market, OrderState
from src.quant_execution_engine.contracts.errors import AmendRejected, StageRejected
from src.quant_execution_engine.core.router import OrderRouter

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import build_client, make_order, make_settings
from tests.unit.adapters.liberator.test_adapter_place import _BASE, _ok_place, make_adapter

_HEALTH = f"{_BASE}/order/health/set"


def _router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: MemStore | None = None,
    adapter: LiberatorAdapter | None = None,
    **settings_overrides: Any,
) -> tuple[OrderRouter, MemStore]:
    store = store or MemStore()
    patch_repositories(monkeypatch, store)
    # EH6: a harness that injects a REAL adapter is modelling an AUTHORIZED node, so it must
    # declare the account it routes -- otherwise routing_authority refuses, correctly. Tests
    # that want the refusal itself live in tests/test_core_routing_authority.py.
    settings_overrides.setdefault("real_routing_accounts", ["ACC-TEST"])
    settings = make_settings(submit_lock_wait_ms=120, **settings_overrides)
    router = OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis(), liberator_adapter=adapter
    )
    return router, store


def _order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "liberator", "price": "123.45"}
    payload.update(overrides)
    return make_order(**payload)


@respx.mock
async def test_required_case_4_idempotent_resubmit_one_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.post(f"{_BASE}/order/place/set").respond(json=_ok_place("3064"))
    router, store = _router(monkeypatch, adapter=make_adapter(), stage="micro_live")
    order = _order()
    first = await router.submit(order)
    assert not first.duplicate
    assert first.result.broker_order_id == "3064"
    assert first.result.engine_state is OrderState.NEW  # fills arrive via reconcile
    second = await router.submit(order)
    assert second.duplicate
    assert second.result == first.result
    assert route.call_count == 1  # the venue saw exactly one request
    assert len(store.orders) == 1


@respx.mock
async def test_required_case_10_paper_intercept_zero_venue_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.post(f"{_BASE}/order/place/set").respond(json=_ok_place())
    router, _ = _router(monkeypatch, adapter=make_adapter(), stage="paper")
    outcome = await router.submit(_order())
    assert outcome.result.broker_order_id is not None
    assert outcome.result.broker_order_id.startswith("SIM-")  # sim ack, real session idle
    assert not route.called


@respx.mock
async def test_required_case_8_breaker_trip_typed_error_and_mass_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolver(client_order_id: str) -> tuple[str, Market] | None:
        return ("B-SEED", Market.SET)

    adapter = make_adapter(breaker_threshold=2, resolve_order=resolver)
    router, store = _router(monkeypatch, adapter=adapter, stage="micro_live")
    resting = _order()
    store.seed(resting, OrderState.NEW)  # open at the venue when the session dies
    respx.post(f"{_BASE}/order/cancelled/set").respond(
        json={"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}
    )
    respx.get(_HEALTH).respond(status_code=503)

    swept: list[tuple[list[str], list[str]]] = []

    async def on_trip() -> None:
        swept.append(await router.mass_cancel())

    await heartbeat_pass(adapter, on_trip=on_trip)
    await heartbeat_pass(adapter, on_trip=on_trip)

    # Mass-cancel was attempted and flattened the resting order.
    assert swept and swept[0][0] == [resting.client_order_id]
    assert store.orders[resting.client_order_id]["status"] is OrderState.CANCELLED
    # New submits for broker=liberator now fail with the typed wire code.
    with pytest.raises(CircuitOpenError) as exc_info:
        await router.submit(_order())
    assert exc_info.value.code == "broker_circuit_open"


async def test_micro_live_without_configured_runtime_is_stage_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, adapter=None, stage="micro_live")
    with pytest.raises(StageRejected):
        await router.submit(_order())
    assert store.orders == {}  # rejected before any durable insert


@respx.mock
async def test_required_case_7_amend_cancel_strictly_before_replacement_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    place_route = respx.post(f"{_BASE}/order/place/set")
    place_route.side_effect = [
        respx.MockResponse(200, json=_ok_place("3064")),
        respx.MockResponse(200, json=_ok_place("3070")),
    ]
    cancel_route = respx.post(f"{_BASE}/order/cancelled/set").respond(
        json={"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}
    )
    router, store = _router(monkeypatch, adapter=make_adapter(), stage="micro_live")
    original = _order()
    await router.submit(original)

    new_cid = str(uuid4())
    outcome = await router.amend(
        original.client_order_id,
        new_client_order_id=new_cid,
        new_price=Decimal("120.50"),
        new_qty=80,  # qty change keeps the PTRM burst signature distinct (see below)
    )

    # Ordering: place(old) -> cancel(old) -> place(new); cancel precedes the new place.
    paths = [call.request.url.path for call in respx.calls]
    assert paths == [
        "/api/v1/order/place/set",
        "/api/v1/order/cancelled/set",
        "/api/v1/order/place/set",
    ]
    assert cancel_route.call_count == 1
    # Old id ends CANCELLED; the new one ran the full pipeline to NEW.
    assert store.orders[original.client_order_id]["status"] is OrderState.CANCELLED
    assert not outcome.duplicate
    assert outcome.result.client_order_id == new_cid
    assert outcome.result.engine_state is OrderState.NEW
    assert outcome.result.broker_order_id == "3070"
    assert store.orders[new_cid]["price"] == Decimal("120.50")
    assert store.orders[new_cid]["quantity"] == 80


@respx.mock
async def test_amend_replacement_gets_no_ptrm_exemption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel_replace replacement re-enters the FULL risk gate with NO
    exemption: with the per-second budget already spent by the original submit,
    the replacement is rate-rejected, the old order is already cancelled (flat,
    never doubled) — the documented cancel_replace consequence.

    (Phase 6 / A3 note: the duplicate-burst fingerprint now includes price, so a
    legitimate re-price no longer self-collides on the burst guard; the
    "no exemption" property is shown here via the rate cap, which still binds.)
    """
    from src.quant_execution_engine.contracts.errors import RiskRejected

    respx.post(f"{_BASE}/order/place/set").respond(json=_ok_place("3064"))
    respx.post(f"{_BASE}/order/cancelled/set").respond(
        json={"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}
    )
    router, store = _router(
        monkeypatch, adapter=make_adapter(), stage="micro_live", risk_max_orders_per_second=1
    )
    original = _order()
    await router.submit(original)  # consumes the 1/s budget for this second
    new_cid = str(uuid4())
    with pytest.raises(RiskRejected, match="order rate"):
        await router.amend(
            original.client_order_id, new_client_order_id=new_cid, new_price=Decimal("120.50")
        )
    assert store.orders[original.client_order_id]["status"] is OrderState.CANCELLED
    assert new_cid not in store.orders  # no doubled exposure


async def test_amend_requires_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _ = _router(monkeypatch, adapter=make_adapter(), stage="micro_live")
    with pytest.raises(AmendRejected, match="new_price and/or new_qty"):
        await router.amend("cid", new_client_order_id=str(uuid4()))


def test_health_and_capabilities_surface_breaker_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.quant_execution_engine.adapters.liberator import runtime

    settings = make_settings()
    client, _ = build_client(settings=settings)
    body = client.get("/health").json()
    assert body["brokers"] is None  # broker-free default

    monkeypatch.setattr(runtime, "_adapter", make_adapter())
    assert get_liberator_adapter() is not None
    body = client.get("/health").json()
    assert body["brokers"]["liberator"]["breaker_state"] == "closed"
    assert body["brokers"]["liberator"]["session_healthy"] is None
    caps = client.get("/capabilities").json()
    assert caps["brokers"]["liberator"]["breaker_state"] == "closed"
    liberator_rows = [c for c in caps["capabilities"] if c["broker"] == "liberator"]
    assert liberator_rows and all(c["adapter_installed"] for c in liberator_rows)

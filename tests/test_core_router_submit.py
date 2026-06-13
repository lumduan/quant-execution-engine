"""OrderRouter: the full submit/cancel/get pipeline over a MemStore."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import respx
from src.quant_execution_engine.adapters.market_data import MarketDataClient
from src.quant_execution_engine.contracts.enums import OrderState, PublicOrderStatus
from src.quant_execution_engine.contracts.errors import (
    CapabilityError,
    ConcurrentSubmit,
    IllegalTransition,
    KillSwitchEngagedError,
    OrderNotFound,
    PriceBandExceeded,
    RiskRejected,
    StageRejected,
)
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.events.hub import EventHub, create_event_hub

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import make_order, make_settings

_MD_BASE = "http://quant-marketdata-engine:8000"
_MD_OHLCV = f"{_MD_BASE}/ohlcv"


def _hub() -> EventHub:
    """Install + return the process-singleton hub for stream assertions."""
    return create_event_hub(make_settings())


def _states_for(hub: EventHub, client_order_id: str) -> list[OrderState]:
    """Engine states streamed for one order, in seq order (via the ring)."""
    return [e.engine_state for e in hub._ring if e.client_order_id == client_order_id]


def _router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: MemStore | None = None,
    redis: Any | None = None,
    **settings_overrides: Any,
) -> tuple[OrderRouter, MemStore, FakeRedis]:
    store = store or MemStore()
    redis = FakeRedis() if redis is None else redis
    patch_repositories(monkeypatch, store)
    settings = make_settings(submit_lock_wait_ms=120, **settings_overrides)
    return OrderRouter(settings=settings, pool=object(), redis=redis), store, redis


async def test_happy_path_full_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    router, store, _ = _router(monkeypatch)
    order = make_order()
    outcome = await router.submit(order)
    assert not outcome.duplicate
    result = outcome.result
    assert result.status is PublicOrderStatus.FILLED
    assert result.engine_state is OrderState.FILLED
    assert result.broker_order_id == f"SIM-{order.client_order_id[:8]}"
    assert result.filled_qty == order.quantity
    assert result.remaining_qty == 0
    assert result.avg_fill_price == order.price


async def test_dedupe_returns_prior_result(monkeypatch: pytest.MonkeyPatch) -> None:
    router, store, _ = _router(monkeypatch)
    order = make_order()
    first = await router.submit(order)
    second = await router.submit(order)
    assert second.duplicate
    assert second.result == first.result
    assert len(store.orders) == 1


async def test_kill_switch_precedes_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    router, store, _ = _router(monkeypatch)
    order = make_order()
    await router.submit(order)
    engaged, _, redis = _router(monkeypatch, store=store, kill_switch_engaged=True)
    with pytest.raises(KillSwitchEngagedError):
        await engaged.submit(order)  # even a known id is rejected


async def test_capability_reject_before_any_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store, _ = _router(monkeypatch)
    order = make_order(broker="liberator", order_type="STOP", stop_price="10")
    with pytest.raises(CapabilityError):
        await router.submit(order)  # liberator+SET has no stop types
    assert store.orders == {}


async def test_risk_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _, _ = _router(monkeypatch, risk_max_order_qty=10)
    with pytest.raises(RiskRejected):
        await router.submit(make_order(quantity=11))


async def test_stage_reject_no_real_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    router, store, _ = _router(monkeypatch, stage="micro_live")
    with pytest.raises(StageRejected):
        await router.submit(make_order())
    assert store.orders == {}


async def test_lock_miss_polls_store_for_the_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store, redis = _router(monkeypatch)
    order = make_order()
    # Another submit holds the lock and has already persisted a lifecycle.
    store.seed(order, OrderState.NEW)
    await redis.set(f"exe:submit:{order.client_order_id}", "other-holder")
    # Make the dedupe miss once so the pipeline reaches the lock.
    real_fetch = store.fetch_order_result
    calls = {"n": 0}

    async def flaky_fetch(pool: Any, cid: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_fetch(pool, cid)

    monkeypatch.setattr(
        "src.quant_execution_engine.db.repositories.fetch_order_result", flaky_fetch
    )
    outcome = await router.submit(order)
    assert outcome.duplicate
    assert outcome.result.engine_state is OrderState.NEW


async def test_lock_miss_times_out_as_concurrent_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, _, redis = _router(monkeypatch)
    order = make_order()
    await redis.set(f"exe:submit:{order.client_order_id}", "other-holder")
    with pytest.raises(ConcurrentSubmit):
        await router.submit(order)


async def test_pk_race_returns_prior_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis lock lost (down) -> INSERT collides -> prior result, not an error."""
    router, store, _ = _router(monkeypatch)
    order = make_order()
    store.seed(order, OrderState.NEW)
    real_fetch = store.fetch_order_result
    calls = {"n": 0}

    async def flaky_fetch(pool: Any, cid: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # dedupe misses; the PK then collides inside the lock
        return await real_fetch(pool, cid)

    monkeypatch.setattr(
        "src.quant_execution_engine.db.repositories.fetch_order_result", flaky_fetch
    )
    outcome = await router.submit(order)
    assert outcome.duplicate
    assert len(store.orders) == 1


async def test_adapter_reject_persists_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    router, store, _ = _router(monkeypatch)
    order = make_order(metadata={"sim_reject": "venue says no"})
    outcome = await router.submit(order)
    assert outcome.result.engine_state is OrderState.REJECTED
    assert outcome.result.status is PublicOrderStatus.REJECTED
    assert outcome.result.reject_reason == "venue says no"


async def test_partial_fills_and_resting(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _, _ = _router(monkeypatch)
    partial = await router.submit(
        make_order(symbol="AAA", quantity=100, metadata={"sim_fills": [40]})
    )
    assert partial.result.engine_state is OrderState.PARTIALLY_FILLED
    assert partial.result.filled_qty == 40
    assert partial.result.remaining_qty == 60
    two_step = await router.submit(
        make_order(symbol="BBB", quantity=100, metadata={"sim_fills": [40, 60]})
    )
    assert two_step.result.engine_state is OrderState.FILLED
    resting = await router.submit(make_order(symbol="CCC", metadata={"sim_fills": []}))
    assert resting.result.engine_state is OrderState.NEW


async def test_ioc_remainder_walks_cancel_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, _, _ = _router(monkeypatch)
    outcome = await router.submit(make_order(tif="IOC", quantity=100, metadata={"sim_fills": [40]}))
    assert outcome.result.engine_state is OrderState.CANCELLED
    assert outcome.result.filled_qty == 40
    assert outcome.result.status is PublicOrderStatus.CANCELLED


async def test_cancel_resting_order(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _, _ = _router(monkeypatch)
    order = make_order(metadata={"sim_fills": []})
    await router.submit(order)
    result = await router.cancel(order.client_order_id)
    assert result.engine_state is OrderState.CANCELLED


async def test_cancel_terminal_unknown_and_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store, _ = _router(monkeypatch)
    filled = make_order()
    await router.submit(filled)
    with pytest.raises(IllegalTransition):
        await router.cancel(filled.client_order_id)
    with pytest.raises(OrderNotFound):
        await router.cancel("missing-id")
    pending = make_order()
    store.seed(pending, OrderState.PENDING_NEW)
    with pytest.raises(IllegalTransition):
        await router.cancel(pending.client_order_id)
    mid_cancel = make_order()
    store.seed(mid_cancel, OrderState.PENDING_CANCEL)
    result = await router.cancel(mid_cancel.client_order_id)  # idempotent re-cancel
    assert result.engine_state is OrderState.PENDING_CANCEL


async def test_mass_cancel_sweeps_open_orders_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store, _ = _router(monkeypatch)
    resting = make_order(symbol="AAA", metadata={"sim_fills": []})
    partial = make_order(symbol="BBB", quantity=100, metadata={"sim_fills": [40]})
    filled = make_order(symbol="CCC")
    for order in (resting, partial, filled):
        await router.submit(order)
    poisoned = make_order(symbol="DDD", metadata={"sim_fills": []})
    await router.submit(poisoned)
    real_update = store.update_status

    async def poison(pool: Any, cid: str, status: OrderState) -> None:
        if cid == poisoned.client_order_id:
            raise RuntimeError("boom")
        await real_update(pool, cid, status)

    monkeypatch.setattr("src.quant_execution_engine.db.repositories.update_status", poison)
    cancelled, failed = await router.mass_cancel()
    assert set(cancelled) == {resting.client_order_id, partial.client_order_id}
    assert failed == [poisoned.client_order_id]
    assert store.orders[filled.client_order_id]["status"] is OrderState.FILLED


async def test_get_unknown_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _, _ = _router(monkeypatch)
    with pytest.raises(OrderNotFound):
        await router.get("nope")


# ------------------------------------------------------ A2 price-band wiring


def _band_router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> tuple[OrderRouter, MemStore]:
    """Router with a market-data client wired in for the price-band check."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    settings = make_settings(
        submit_lock_wait_ms=120,
        price_band_enabled=enabled,
        risk_max_orders_per_second=100,
        risk_max_order_value=Decimal("1000000000"),  # headroom: isolate the band check
    )
    router = OrderRouter(
        settings=settings,
        pool=object(),
        redis=FakeRedis(),
        market_data_client=MarketDataClient(_MD_BASE, None),
    )
    return router, store


@respx.mock
async def test_price_band_rejects_through_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(_MD_OHLCV).respond(json={"bars": [{"ts": "2026-06-11T00:00:00Z", "close": "100"}]})
    router, store = _band_router(monkeypatch, enabled=True)
    with pytest.raises(PriceBandExceeded):
        await router.submit(make_order(symbol="PTT", price="200"))  # +100% ≫ 10%
    assert store.orders == {}  # rejected before any insert (after the risk gate)


@respx.mock
async def test_price_band_within_band_submits(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(_MD_OHLCV).respond(json={"bars": [{"ts": "2026-06-11T00:00:00Z", "close": "100"}]})
    router, _ = _band_router(monkeypatch, enabled=True)
    outcome = await router.submit(make_order(symbol="PTT", price="105"))  # +5% < 10%
    assert outcome.result.engine_state is OrderState.FILLED


async def test_price_band_disabled_never_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    # No respx route: a disabled band must not touch market data even far off-price.
    router, _ = _band_router(monkeypatch, enabled=False)
    outcome = await router.submit(make_order(symbol="PTT", price="99999"))
    assert outcome.result.engine_state is OrderState.FILLED


# ------------------------------------------------------- Phase-5 stream events


async def test_submit_threads_strategy_id_to_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hub()
    router, store, _ = _router(monkeypatch)
    order = make_order(metadata={"sim_fills": []})
    await router.submit(order, strategy_id="csm")
    assert store.orders[order.client_order_id]["strategy_id"] == "csm"


async def test_golden_path_streams_pending_new_new_filled_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _hub()
    router, _, _ = _router(monkeypatch)
    order = make_order()  # default ⇒ single full fill
    await router.submit(order, strategy_id="csm")
    assert _states_for(hub, order.client_order_id) == [
        OrderState.PENDING_NEW,
        OrderState.NEW,
        OrderState.FILLED,
    ]
    # Every event carries the strategy attribution.
    assert all(
        e.strategy_id == "csm" for e in hub._ring if e.client_order_id == order.client_order_id
    )


async def test_partial_fill_plan_streams_one_event_per_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _hub()
    router, _, _ = _router(monkeypatch)
    order = make_order(quantity=100, metadata={"sim_fills": [40, 60]})
    await router.submit(order)
    assert _states_for(hub, order.client_order_id) == [
        OrderState.PENDING_NEW,
        OrderState.NEW,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]


async def test_kill_switch_mass_cancel_streams_pending_cancel_then_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _hub()
    router, _, _ = _router(monkeypatch)
    resting = make_order(symbol="AAA", metadata={"sim_fills": []})
    await router.submit(resting)
    cancelled, failed = await router.mass_cancel()
    assert cancelled == [resting.client_order_id] and failed == []
    # The sweep emits PENDING_CANCEL then CANCELLED on the stream (success criterion).
    states = _states_for(hub, resting.client_order_id)
    assert states[-2:] == [OrderState.PENDING_CANCEL, OrderState.CANCELLED]


async def test_rejected_order_streams_pending_new_then_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _hub()
    router, _, _ = _router(monkeypatch)
    order = make_order(metadata={"sim_reject": "venue says no"})
    await router.submit(order)
    assert _states_for(hub, order.client_order_id) == [
        OrderState.PENDING_NEW,
        OrderState.REJECTED,
    ]

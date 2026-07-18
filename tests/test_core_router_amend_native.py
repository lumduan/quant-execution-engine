"""Native-amend orchestration over MemStore (router.amend native branch).

After the broker-023 removal the ``native`` amend branch is exercised only by
the ``sim`` broker (no REAL broker declares native — Liberator + Streaming Pro
both cancel_replace). These cover the frozen PENDING_REPLACE -> NEW edge via
broker=sim: accept (same cid, updated price/qty), the two-step PARTIALLY_FILLED
restore, venue amend-reject as a non-terminal restore (never REJECTED,
reject_reason untouched — driven by monkeypatching ``SimAdapter.amend``), the
pre-flight rules (status preconditions, qty/display_qty checks, cid asymmetry),
the PTRM re-check with NO exemption, and the kill-switch asymmetry (gates amend,
not cancel). The cancel_replace branch is exercised for the broker=liberator and
broker=streaming_pro rows to prove the keeper amend path is preserved through the
branch dispatch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from src.quant_execution_engine.adapters.base import AmendAck
from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.adapters.sim import SimAdapter
from src.quant_execution_engine.contracts.enums import Broker, Market, OrderState
from src.quant_execution_engine.contracts.errors import (
    AmendRejected,
    IllegalTransition,
    KillSwitchEngagedError,
    OrderNotFound,
    RiskRejected,
)
from src.quant_execution_engine.core.router import OrderRouter

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import make_order, make_settings


def _router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: MemStore | None = None,
    redis: Any | None = None,
    **settings_overrides: Any,
) -> tuple[OrderRouter, MemStore]:
    store = store or MemStore()
    patch_repositories(monkeypatch, store)
    settings = make_settings(submit_lock_wait_ms=120, **settings_overrides)
    router = OrderRouter(
        settings=settings,
        pool=object(),
        redis=FakeRedis() if redis is None else redis,
    )
    return router, store


def order_now() -> datetime:
    return datetime.now(UTC)


def _native_order(**overrides: Any) -> Any:
    """A resting sim (native-amend) SET LIMIT order; override any field."""
    payload: dict[str, Any] = {"broker": "sim", "price": "100.00", "quantity": 100}
    payload.update(overrides)
    return make_order(**payload)


# ----------------------------------------------------------------- accept flows


async def test_native_accept_updates_price_and_qty_same_cid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id

    outcome = await router.amend(cid, new_price=Decimal("105.50"), new_qty=80)

    assert not outcome.duplicate
    assert outcome.result.client_order_id == cid  # native keeps the same id
    assert outcome.result.engine_state is OrderState.NEW
    assert store.orders[cid]["status"] is OrderState.NEW
    assert store.orders[cid]["price"] == Decimal("105.50")
    assert store.orders[cid]["quantity"] == 80


async def test_native_accept_price_only_keeps_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order(quantity=100)
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id

    await router.amend(cid, new_price=Decimal("99.00"))

    assert store.orders[cid]["price"] == Decimal("99.00")
    assert store.orders[cid]["quantity"] == 100  # COALESCE keeps the old qty


async def test_native_accept_partial_fill_restores_partially_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """filled_qty>0 ⇒ the two-step restore lands on PARTIALLY_FILLED, not NEW."""
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order(quantity=100)
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id
    # Record a partial fill (NEW -> PARTIALLY_FILLED) before the amend.
    await store.apply_fill(
        object(),
        cid,
        broker_fill_id="F1",
        price=Decimal("100.00"),
        quantity=40,
        exec_ts=order_now(),
        total_quantity=100,
    )
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED

    outcome = await router.amend(cid, new_price=Decimal("101.00"))

    assert outcome.result.engine_state is OrderState.PARTIALLY_FILLED
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED
    assert store.orders[cid]["price"] == Decimal("101.00")


# -------------------------------------------------------------- venue rejection
# The only native broker is ``sim``; its SimAdapter always acks ok, so a venue
# amend-reject is exercised by monkeypatching ``SimAdapter.amend``.


async def test_native_venue_reject_restores_new_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Decimal | None, int | None]] = []

    async def _reject(
        _self: SimAdapter,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        calls.append((client_order_id, new_price, new_qty))
        return AmendAck(ok=False, semantics="native", reason="partial race")

    monkeypatch.setattr(SimAdapter, "amend", _reject)
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id

    with pytest.raises(AmendRejected, match="partial race"):
        await router.amend(cid, new_price=Decimal("105.50"))

    # Order is still LIVE: restored to NEW, price unchanged, reject_reason untouched.
    assert store.orders[cid]["status"] is OrderState.NEW
    assert store.orders[cid]["status"] is not OrderState.REJECTED
    assert store.orders[cid]["price"] == Decimal("100.00")
    assert store.orders[cid]["reject_reason"] is None
    assert calls == [(cid, Decimal("105.50"), None)]


async def test_native_venue_reject_partial_restores_partially_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _reject(
        _self: SimAdapter,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        return AmendAck(ok=False, semantics="native", reason="rejected")

    monkeypatch.setattr(SimAdapter, "amend", _reject)
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order(quantity=100)
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id
    await store.apply_fill(
        object(),
        cid,
        broker_fill_id="F1",
        price=Decimal("100.00"),
        quantity=30,
        exec_ts=order_now(),
        total_quantity=100,
    )

    with pytest.raises(AmendRejected):
        await router.amend(cid, new_price=Decimal("105.50"))

    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED


async def test_native_adapter_error_treated_as_venue_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any AdapterError from adapter.amend is belt-and-braces ack-not-ok."""

    async def _boom(
        _self: SimAdapter,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        raise AdapterError("transport blew up")

    monkeypatch.setattr(SimAdapter, "amend", _boom)
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id

    with pytest.raises(AmendRejected, match="transport blew up"):
        await router.amend(cid, new_price=Decimal("105.50"))
    assert store.orders[cid]["status"] is OrderState.NEW


# ----------------------------------------------------------------- preconditions


async def test_native_no_change_request_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, OrderState.NEW)
    with pytest.raises(AmendRejected, match="new_price and/or new_qty"):
        await router.amend(order.client_order_id)


async def test_native_unknown_order_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    router, _ = _router(monkeypatch, stage="sim")
    with pytest.raises(OrderNotFound):
        await router.amend(str(uuid4()), new_price=Decimal("1"))


@pytest.mark.parametrize(
    "status",
    [
        OrderState.PENDING_NEW,
        OrderState.PENDING_CANCEL,
        OrderState.FILLED,
        OrderState.PENDING_REPLACE,
    ],
)
async def test_native_preconditions_reject_non_amendable_states(
    monkeypatch: pytest.MonkeyPatch, status: OrderState
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, status)
    with pytest.raises(IllegalTransition):
        await router.amend(order.client_order_id, new_price=Decimal("105"))


async def test_native_pending_replace_reports_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, OrderState.PENDING_REPLACE)
    with pytest.raises(IllegalTransition, match="amend already in flight"):
        await router.amend(order.client_order_id, new_price=Decimal("105"))


async def test_native_new_qty_at_or_below_filled_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order(quantity=100)
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id
    await store.apply_fill(
        object(),
        cid,
        broker_fill_id="F1",
        price=Decimal("100.00"),
        quantity=60,
        exec_ts=order_now(),
        total_quantity=100,
    )
    with pytest.raises(AmendRejected, match="filled quantity"):
        await router.amend(cid, new_qty=60)  # 60 <= 60 filled
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED  # untouched


async def test_native_new_qty_below_display_qty_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order(order_type="ICEBERG", display_qty=50, quantity=100)
    store.seed(order, OrderState.NEW)
    with pytest.raises(AmendRejected, match="display_qty"):
        await router.amend(order.client_order_id, new_qty=40)  # 40 < display_qty 50


# --------------------------------------------------------------- cid asymmetry


async def test_native_broker_with_new_client_order_id_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, OrderState.NEW)
    with pytest.raises(AmendRejected, match="omit new_client_order_id"):
        await router.amend(
            order.client_order_id, new_client_order_id=str(uuid4()), new_price=Decimal("105")
        )


async def test_cancel_replace_broker_without_new_cid_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, store = _router(monkeypatch, stage="sim")
    order = make_order(broker="liberator", price="100.00")
    store.seed(order, OrderState.NEW)
    with pytest.raises(AmendRejected, match="requires new_client_order_id"):
        await router.amend(order.client_order_id, new_price=Decimal("105"))


async def test_cancel_replace_happy_path_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel_replace branch still cancels the old + submits the replacement (Liberator)."""
    router, store = _router(monkeypatch, stage="sim")
    order = make_order(broker="liberator", price="100.00", quantity=100)
    store.seed(order, OrderState.NEW)
    new_cid = str(uuid4())

    outcome = await router.amend(
        order.client_order_id,
        new_client_order_id=new_cid,
        new_price=Decimal("99.00"),
        new_qty=80,
    )

    assert store.orders[order.client_order_id]["status"] is OrderState.CANCELLED
    assert outcome.result.client_order_id == new_cid  # the replacement cid
    assert store.orders[new_cid]["price"] == Decimal("99.00")
    assert store.orders[new_cid]["quantity"] == 80


async def test_cancel_replace_happy_path_streaming_pro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel_replace branch also serves broker=streaming_pro (a keeper broker)."""
    router, store = _router(monkeypatch, stage="sim")
    order = make_order(broker="streaming_pro", price="100.00", quantity=100)
    store.seed(order, OrderState.NEW)
    new_cid = str(uuid4())

    outcome = await router.amend(
        order.client_order_id,
        new_client_order_id=new_cid,
        new_price=Decimal("99.00"),
        new_qty=80,
    )

    assert store.orders[order.client_order_id]["status"] is OrderState.CANCELLED
    assert outcome.result.client_order_id == new_cid
    assert store.orders[new_cid]["price"] == Decimal("99.00")
    assert store.orders[new_cid]["quantity"] == 80


# -------------------------------------------------------------- PTRM no-exemption


async def test_native_ptrm_no_exemption_leaves_original_resting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A risk-rejected amend leaves the original order untouched (still resting)."""
    router, store = _router(monkeypatch, stage="sim", risk_max_order_qty=50)
    order = _native_order(quantity=40)
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id

    with pytest.raises(RiskRejected):
        await router.amend(cid, new_qty=60)  # 60 > max_order_qty 50

    # Nothing was sent; the original is still NEW with its original quantity.
    assert store.orders[cid]["status"] is OrderState.NEW
    assert store.orders[cid]["quantity"] == 40


# ----------------------------------------------------------------- kill-switch


async def test_kill_switch_blocks_amend_but_not_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented asymmetry: amends can raise exposure (gated); cancels reduce it."""
    router, store = _router(monkeypatch, stage="sim", kill_switch_engaged=True)
    order = _native_order()
    store.seed(order, OrderState.NEW)
    cid = order.client_order_id

    with pytest.raises(KillSwitchEngagedError):
        await router.amend(cid, new_price=Decimal("105"))
    assert store.orders[cid]["status"] is OrderState.NEW  # amend never started

    # The cancel path stays un-gated even with the kill-switch engaged.
    result = await router.cancel(cid)
    assert result.engine_state is OrderState.CANCELLED


# ---------------------------------------------------------------- sim semantics


async def test_sim_amends_native_against_simadapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """broker=sim declares native amend and routes the native branch onto SimAdapter."""
    # Verify (don't modify) SimAdapter declares native amend semantics.
    sim = SimAdapter(default_fill_price=Decimal("1"))
    ack = await sim.amend("cid", new_price=Decimal("2"))
    assert ack.ok and ack.semantics == "native"
    # And the capability row that drives the branch is native for both books.
    from src.quant_execution_engine.contracts import capabilities

    assert capabilities.lookup(Broker.SIM, Market.SET).amend == "native"

    router, store = _router(monkeypatch, stage="sim")
    order = _native_order()
    store.seed(order, OrderState.NEW)
    outcome = await router.amend(order.client_order_id, new_price=Decimal("105"))
    assert outcome.result.client_order_id == order.client_order_id

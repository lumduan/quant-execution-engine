"""Phase 6 / C1 — submit-interrupt idempotency soak (no double-send under fault).

Adversarial fault injection over the EXISTING persistence + reconciliation logic
(the broker-facing layer is respx-mocked; the engine's durable store is driven via
MemStore). Each scenario simulates a process death mid-submit and asserts the
engine resolves safely — never stuck in PENDING_NEW, never an illegal transition,
never a duplicate placement.

* S1 PENDING_NEW stuck     — venue match → ack to NEW; no match within the bounded
                             window → REJECTED. Never re-sends.
* S2 ack lost              — broker accepted (has an orderNo) but the ack was never
                             persisted; reconcile acks → broker_order_id + NEW.
* S3 fill before ack       — a fill cannot illegally jump PENDING_NEW → FILLED; it
                             is held (apply_fill requires NEW/PARTIALLY_FILLED).
* S4 duplicate-submit retry — the same cid after a restart returns the prior ack,
                             duplicate=True, no re-route / re-insert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from src.quant_execution_engine.adapters.liberator.reconciler import LiberatorReconciler
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.contracts.errors import IllegalTransition
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.models import OrderRow

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import make_order, make_settings
from tests.unit.adapters.liberator.test_adapter_place import _BASE, make_adapter

_NOW = datetime(2026, 6, 13, 8, 0, 0, tzinfo=UTC)


def _seed(
    store: MemStore, status: OrderState, *, age_seconds: float = 10.0, **overrides: Any
) -> OrderRow:
    order = make_order(broker="liberator", price="123.45", **overrides)
    store.seed(order, status)
    raw = store.orders[order.client_order_id]
    raw["created_at"] = _NOW - timedelta(seconds=age_seconds)
    # A PENDING_NEW lost-ack row never carries a venue id yet.
    if status is OrderState.PENDING_NEW:
        raw["broker_order_id"] = None
    return OrderRow(**raw)


def _reconciler() -> LiberatorReconciler:
    return LiberatorReconciler(
        make_adapter(),
        interval_seconds=12,
        pool_provider=lambda: object(),  # MemStore-patched repositories ignore the pool
        now=lambda: _NOW,
    )


def _orders_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {"list": items}}}


def _venue_json(row: OrderRow, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "orderNo": row.broker_order_id or "VEN-1",
        "accountNo": row.account,
        "symbol": row.symbol,
        "side": "B" if row.side.value == "BUY" else "S",
        "volume": row.quantity,
        "matched": 0,
        "balance": row.quantity,
        "cancelled": 0,
        "price": "123.45",
        "status": "PENDING",
        "statusShow": "O",
        "rejectCode": "",
        "entryTime": (row.created_at + timedelta(seconds=2)).isoformat(),
    }
    base.update(overrides)
    return base


# ----------------------------------------- S1 — PENDING_NEW stuck (never wedged)


@respx.mock
async def test_s1_pending_new_stuck_acks_on_fuzzy_match_no_resend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck PENDING_NEW with a venue match within ±5 s acks to NEW; no re-place."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _seed(store, OrderState.PENDING_NEW, age_seconds=10)  # >5 s, no broker id
    cid = row.client_order_id
    # The venue row matches (symbol, qty, side) with an entryTime inside the window
    # but carries a DIFFERENT orderNo (the ack we never saw).
    orders_route = respx.get(f"{_BASE}/orders/{row.account}").respond(
        json=_orders_response([_venue_json(row, orderNo="LOST-ACK-9")])
    )
    place_route = respx.post(f"{_BASE}/order/place/set")

    applied = await _reconciler().reconcile_once()

    assert applied == 1
    assert store.orders[cid]["status"] is OrderState.NEW  # never wedged in PENDING_NEW
    assert store.orders[cid]["broker_order_id"] == "LOST-ACK-9"
    assert orders_route.called
    assert not place_route.called  # NEVER re-sends


@respx.mock
async def test_s1_pending_new_unmatched_resolves_bounded_to_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No venue match past the bounded window → REJECTED, not stuck forever."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    # Inside the window: the loop waits (no transition, no re-send).
    young = _seed(store, OrderState.PENDING_NEW, age_seconds=30)
    place_route = respx.post(f"{_BASE}/order/place/set")
    respx.get(f"{_BASE}/orders/{young.account}").respond(json=_orders_response([]))
    assert await _reconciler().reconcile_once() == 0
    assert store.orders[young.client_order_id]["status"] is OrderState.PENDING_NEW

    # Past the bounded timeout: resolved to REJECTED so routing is never wedged.
    old = _seed(store, OrderState.PENDING_NEW, age_seconds=120, symbol="AAA")
    respx.get(f"{_BASE}/orders/{old.account}").respond(json=_orders_response([]))
    await _reconciler().reconcile_once()
    assert store.orders[old.client_order_id]["status"] is OrderState.REJECTED
    assert store.orders[old.client_order_id]["reject_reason"] == "ack_lost_unmatched"
    assert not place_route.called  # NEVER re-sends on either pass


# ------------------------------------------ S2 — ack lost after broker accepted


@respx.mock
async def test_s2_ack_lost_after_broker_accepted_persists_broker_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker accepted (venue shows the orderNo) but the ack was never persisted."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _seed(store, OrderState.PENDING_NEW, age_seconds=10)
    cid = row.client_order_id
    respx.get(f"{_BASE}/orders/{row.account}").respond(
        json=_orders_response([_venue_json(row, orderNo="ACCEPTED-42")])
    )
    place_route = respx.post(f"{_BASE}/order/place/set")

    await _reconciler().reconcile_once()

    assert store.orders[cid]["status"] is OrderState.NEW
    assert store.orders[cid]["broker_order_id"] == "ACCEPTED-42"
    assert not place_route.called


# ------------------------------------------------ S3 — fill before ack (held)


async def test_s3_fill_before_ack_cannot_jump_pending_new_to_filled() -> None:
    """A fill for a still-PENDING_NEW order must NOT illegally advance the state.

    ``apply_fill`` flips the state via the frozen edges, and there is no
    PENDING_NEW → FILLED / PARTIALLY_FILLED edge — so the fill is held (the
    transition is rejected as illegal), never silently applied. No crash.
    """
    store = MemStore()
    row = _seed(store, OrderState.PENDING_NEW, age_seconds=2)
    cid = row.client_order_id
    with pytest.raises(IllegalTransition):
        await store.apply_fill(
            object(),
            cid,
            broker_fill_id="F-EARLY",
            price=row.price or Decimal("1"),
            quantity=row.quantity,
            exec_ts=_NOW,
            total_quantity=row.quantity,
        )
    # The order is unchanged — still awaiting its ack (the reconciler repairs it).
    assert store.orders[cid]["status"] is OrderState.PENDING_NEW
    assert store.fills.get(cid, []) == []  # the early fill was not recorded as a flip


async def test_s3_fill_lands_cleanly_once_the_ack_arrives() -> None:
    """After the ack (PENDING_NEW → NEW), the same fill applies via a legal edge."""
    store = MemStore()
    row = _seed(store, OrderState.PENDING_NEW, age_seconds=2)
    cid = row.client_order_id
    await store.ack_order(object(), cid, "VEN-7")  # the ack lands first
    state = await store.apply_fill(
        object(),
        cid,
        broker_fill_id="F-LATE",
        price=row.price or Decimal("1"),
        quantity=row.quantity,
        exec_ts=_NOW,
        total_quantity=row.quantity,
    )
    assert state is OrderState.FILLED
    assert store.orders[cid]["status"] is OrderState.FILLED


# -------------------------------------------- S4 — duplicate submit after restart


async def test_s4_duplicate_submit_same_cid_returns_prior_ack_no_reroute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry with the same cid (post-restart) returns the prior ack, no re-route.

    The store is pre-seeded with a FILLED order (as if a prior process completed
    it). ``router.submit`` hits the dedupe path BEFORE any adapter call: it returns
    ``duplicate=True`` with the prior result and never inserts or routes again.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    order = make_order(broker="sim", price="35.50")
    store.seed(order, OrderState.FILLED)  # the prior, completed order
    store.orders[order.client_order_id]["broker_order_id"] = "PRIOR-1"
    store.fills[order.client_order_id] = [
        {"broker_fill_id": "f1", "price": order.price, "quantity": order.quantity, "exec_ts": _NOW}
    ]
    before = dict(store.orders[order.client_order_id])

    router = OrderRouter(settings=make_settings(), pool=object(), redis=FakeRedis())
    outcome = await router.submit(order)

    assert outcome.duplicate is True
    assert outcome.result.broker_order_id == "PRIOR-1"
    assert outcome.result.engine_state is OrderState.FILLED
    # No re-route / re-insert: the row is byte-for-byte the pre-seeded one.
    assert len(store.orders) == 1
    assert store.orders[order.client_order_id] == before


async def test_s4_idempotent_resubmit_via_durable_pk_when_dedupe_read_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the first dedupe read misses, the durable PK collision returns prior.

    Drives the router's ``DuplicateOrderSignal`` branch: ``insert_order`` raises a
    PK collision (the durable backstop), and ``submit`` re-reads and returns the
    prior result with ``duplicate=True`` — still no re-route. A monkeypatched
    ``insert_order`` simulates the row already existing under a concurrent writer.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    order = make_order(broker="sim", price="35.50")

    async def _raise_dup(pool: Any, o: Any, strategy_id: Any = None) -> None:
        from src.quant_execution_engine.db.errors import DuplicateOrderSignal

        # Make the prior row visible (as a concurrent insert would) before raising.
        store.seed(o, OrderState.NEW)
        store.orders[o.client_order_id]["broker_order_id"] = "CONCURRENT-1"
        raise DuplicateOrderSignal(o.client_order_id)

    monkeypatch.setattr(repositories, "insert_order", _raise_dup)
    router = OrderRouter(settings=make_settings(), pool=object(), redis=FakeRedis())
    outcome = await router.submit(order)
    assert outcome.duplicate is True
    assert outcome.result.broker_order_id == "CONCURRENT-1"
    assert len(store.orders) == 1

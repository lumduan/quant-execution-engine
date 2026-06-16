"""Reconciler v1: plan_actions table + fuzzy match + full passes over MemStore + respx venue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import respx
from src.quant_execution_engine.adapters.streaming_pro.models import VenueOrderRow
from src.quant_execution_engine.adapters.streaming_pro.reconciler import (
    StreamingProReconciler,
    fill_price_for,
    fuzzy_match,
    plan_actions,
)
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.db.models import OrderRow

from tests._fakes import MemStore, patch_repositories
from tests.conftest import make_order
from tests.unit.adapters.streaming_pro.test_adapter_place import _BASE, make_adapter

_NOW = datetime(2026, 6, 16, 8, 0, 0, tzinfo=UTC)


def _row(
    store: MemStore, status: OrderState, *, age_seconds: float = 0.0, **overrides: Any
) -> OrderRow:
    order = make_order(broker="streaming_pro", price="32.47", **overrides)
    store.seed(order, status)
    raw = store.orders[order.client_order_id]
    raw["created_at"] = _NOW - timedelta(seconds=age_seconds)
    return OrderRow(**raw)


def _item(row: OrderRow, **overrides: Any) -> VenueOrderRow:
    payload: dict[str, Any] = {
        "orderNo": row.broker_order_id or "8962991",
        "accountNo": row.account,
        "symbol": row.symbol,
        "side": "Buy",
        "qty": row.quantity,
        "matchQty": 0,
        "balanceQty": row.quantity,
        "cancelQty": 0,
        "price": str(row.price or "0"),
        "status": "S",
        "showStatus": "Pending(S)",
        "rejectReason": "",
        "entryTime": (row.created_at + timedelta(seconds=2)).isoformat(),
    }
    payload.update(overrides)
    return VenueOrderRow.model_validate(payload)


# ----------------------------------------------------------------- plan_actions


def test_pending_new_with_match_acks_then_carries_fills() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW)
    actions = plan_actions(row, 0, _item(row, matchQty=40, balanceQty=60), now=_NOW)
    assert [a.kind for a in actions] == ["ack", "fill"]
    assert actions[1].fill_qty == 40


def test_pending_new_venue_reject_skips_ack() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW)
    actions = plan_actions(row, 0, _item(row, rejectReason="no margin"), now=_NOW)
    assert [a.kind for a in actions] == ["reject"]


def test_pending_new_unmatched_waits_then_resolves_bounded() -> None:
    store = MemStore()
    young = _row(store, OrderState.PENDING_NEW, age_seconds=2)
    assert plan_actions(young, 0, None, now=_NOW) == []  # inside the window: wait
    old = _row(store, OrderState.PENDING_NEW, age_seconds=120)
    actions = plan_actions(old, 0, None, now=_NOW)
    assert [a.kind for a in actions] == ["reject"]
    assert actions[0].reason == "ack_lost_unmatched"


def test_fill_delta_is_cumulative_watermark() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    actions = plan_actions(row, 40, _item(row, matchQty=70, balanceQty=30), now=_NOW)
    assert [a.kind for a in actions] == ["fill"]
    assert actions[0].fill_qty == 30
    assert actions[0].fill_id is not None and actions[0].fill_id.endswith(":70")
    assert plan_actions(row, 70, _item(row, matchQty=70), now=_NOW) == []  # no new delta


def test_full_match_emits_fill_without_terminal() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    actions = plan_actions(row, 0, _item(row, matchQty=100, balanceQty=0), now=_NOW)
    assert [a.kind for a in actions] == ["fill"]


def test_venue_cancel_expire_and_post_ack_reject() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    assert [a.kind for a in plan_actions(row, 0, _item(row, status="Cancelled"), now=_NOW)] == [
        "cancel_two_step"
    ]
    assert [a.kind for a in plan_actions(row, 0, _item(row, showStatus="Expired"), now=_NOW)] == [
        "expire"
    ]
    actions = plan_actions(row, 0, _item(row, rejectReason="late reject"), now=_NOW)
    assert [a.kind for a in actions] == ["post_ack_reject"]


def test_pending_cancel_confirms_or_waits() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_CANCEL)
    assert [a.kind for a in plan_actions(row, 0, None, now=_NOW)] == ["cancel_confirm"]
    assert [a.kind for a in plan_actions(row, 0, _item(row, status="Cancelled"), now=_NOW)] == [
        "cancel_confirm"
    ]
    assert plan_actions(row, 0, _item(row), now=_NOW) == []  # still live — wait
    assert (
        plan_actions(row, 0, _item(row, matchQty=5), now=_NOW) == []
    )  # late fill surfaced not kept


def test_terminal_states_with_no_venue_row() -> None:
    store = MemStore()
    assert plan_actions(_row(store, OrderState.NEW, age_seconds=120), 0, None, now=_NOW) == []


def test_fill_price_fallbacks() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    assert fill_price_for(row, _item(row, price="42.50")) == Decimal("42.50")
    assert fill_price_for(row, _item(row, price="0")) == row.price


# ----------------------------------------------------------------- fuzzy match


def test_fuzzy_match_unique_within_window() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    inside = _item(
        row, orderNo="7001", entryTime=(row.created_at + timedelta(seconds=3)).isoformat()
    )
    outside = _item(
        row, orderNo="7002", entryTime=(row.created_at + timedelta(seconds=9)).isoformat()
    )
    wrong_qty = _item(row, orderNo="7003", qty=row.quantity + 1)
    assert fuzzy_match(row, [inside, outside, wrong_qty], claimed_order_nos=set()) is inside


def test_fuzzy_match_ambiguous_or_claimed() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    ts = (row.created_at + timedelta(seconds=1)).isoformat()
    a = _item(row, orderNo="7001", entryTime=ts)
    b = _item(row, orderNo="7002", entryTime=ts)
    assert fuzzy_match(row, [a, b], claimed_order_nos=set()) is None  # ambiguous: never guess
    assert fuzzy_match(row, [a, b], claimed_order_nos={"7002"}) is a  # claimed excluded
    no_ts = _item(row, orderNo="7005", entryTime=None)
    assert fuzzy_match(row, [no_ts], claimed_order_nos=set()) is None


# ------------------------------------------------------------- reconcile_once


def _reconciler() -> StreamingProReconciler:
    return StreamingProReconciler(
        make_adapter(), interval_seconds=12, pool_provider=lambda: object(), now=lambda: _NOW
    )


def _route(account: str, market: str) -> Any:
    return respx.get(f"{_BASE}/orders", params={"account": account, "market": market})


@respx.mock
async def test_full_lifecycle_driven_by_polls(monkeypatch: Any) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    cid = row.client_order_id
    _route(row.account, "TFEX").respond(json=[])
    set_route = _route(row.account, "SET")

    set_route.respond(json=[_item(row, orderNo="3064").model_dump(by_alias=True, mode="json")])
    await _reconciler().reconcile_once()
    assert store.orders[cid]["status"] is OrderState.NEW
    assert store.orders[cid]["broker_order_id"] == "3064"

    full = _item(row, orderNo="3064", matchQty=100, balanceQty=0)
    set_route.respond(json=[full.model_dump(by_alias=True, mode="json")])
    await _reconciler().reconcile_once()
    assert store.orders[cid]["status"] is OrderState.FILLED
    assert [(f["broker_fill_id"], f["quantity"]) for f in store.fills[cid]] == [("3064:100", 100)]

    applied = await _reconciler().reconcile_once()  # idempotent re-poll
    assert applied == 0


@respx.mock
async def test_lost_ack_fuzzy_match_advances_to_new(monkeypatch: Any) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)  # stuck > 5 s, no broker id
    assert store.orders[row.client_order_id]["broker_order_id"] is None
    lost = _item(
        row, orderNo="9-LOST", entryTime=(row.created_at + timedelta(seconds=2)).isoformat()
    )
    _route(row.account, "SET").respond(json=[lost.model_dump(by_alias=True, mode="json")])
    _route(row.account, "TFEX").respond(json=[])
    await _reconciler().reconcile_once()
    assert store.orders[row.client_order_id]["status"] is OrderState.NEW
    assert store.orders[row.client_order_id]["broker_order_id"] == "9-LOST"


@respx.mock
async def test_reconcile_skips_group_on_transport_error(monkeypatch: Any) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    _route(row.account, "SET").respond(status_code=503)
    _route(row.account, "TFEX").respond(json=[])
    assert await _reconciler().reconcile_once() == 0  # group skipped, never crashes
    assert store.orders[row.client_order_id]["status"] is OrderState.PENDING_NEW


async def test_reconcile_no_rows_is_zero(monkeypatch: Any) -> None:
    patch_repositories(monkeypatch, MemStore())
    assert await _reconciler().reconcile_once() == 0

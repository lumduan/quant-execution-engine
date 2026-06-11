"""Reconciler v1: plan_actions table (+ replace_resolve) + grouped passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from src.quant_execution_engine.adapters.settrade.client import RateBudget
from src.quant_execution_engine.adapters.settrade.models import SettradeOrderItem
from src.quant_execution_engine.adapters.settrade.reconciler import (
    SettradeReconciler,
    fill_price_for,
    fuzzy_match,
    plan_actions,
)
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.db.models import OrderRow

from tests._fakes import MemStore, patch_repositories
from tests.conftest import make_order
from tests.unit.adapters.settrade.test_adapter_place import (
    _BASE,
    _BROKER,
    _login_route,
    make_adapter,
)

_NOW = datetime(2026, 6, 11, 8, 0, 0, tzinfo=UTC)


def _row(
    store: MemStore, status: OrderState, *, age_seconds: float = 0.0, **overrides: Any
) -> OrderRow:
    order = make_order(broker="settrade", price="100.00", **overrides)
    store.seed(order, status)
    raw = store.orders[order.client_order_id]
    raw["created_at"] = _NOW - timedelta(seconds=age_seconds)
    return OrderRow(**raw)


def _item(row: OrderRow, **overrides: Any) -> SettradeOrderItem:
    payload: dict[str, Any] = {
        "orderNo": row.broker_order_id or "SET-1",
        "accountNo": row.account,
        "symbol": row.symbol,
        "side": "Buy" if row.side.value == "BUY" else "Sell",
        "vol": row.quantity,
        "matched": 0,
        "balance": row.quantity,
        "cancelled": 0,
        "price": float(row.price or 0),
        "status": "O",
        "rejectCode": 0,
    }
    payload.update(overrides)
    return SettradeOrderItem.model_validate(payload)


# ----------------------------------------------------------------- plan_actions


def test_pending_new_with_match_acks_then_carries_fills_and_terminals() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    item = _item(row, orderNo="SET-9", matched=40, cancelled=60, balance=0, status="CS")
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["ack", "fill", "cancel_two_step"]
    assert actions[0].broker_order_id == "SET-9"
    assert actions[1].fill_qty == 40
    assert actions[1].fill_id == "SET-9:40"


def test_pending_new_venue_reject_goes_straight_to_rejected_without_ack() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    item = _item(row, rejectCode="105", rejectReason="bad symbol")
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["reject"]
    assert actions[0].reason is not None and "105" in actions[0].reason


def test_fill_delta_is_cumulative_watermark() -> None:
    store = MemStore()
    row = _row(store, OrderState.PARTIALLY_FILLED)
    item = _item(row, orderNo="B-SEED", matched=70, balance=30)
    actions = plan_actions(row, 40, item, now=_NOW)
    assert [a.kind for a in actions] == ["fill"]
    assert actions[0].fill_qty == 30
    assert actions[0].fill_id == "B-SEED:70"
    assert actions[0].total_quantity == row.quantity
    assert plan_actions(row, 70, item, now=_NOW) == []  # no progress, no actions


def test_full_match_emits_fill_without_terminal_actions() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    item = _item(row, orderNo="B-SEED", matched=row.quantity, balance=0)
    assert [a.kind for a in plan_actions(row, 0, item, now=_NOW)] == ["fill"]


def test_venue_cancel_and_expire_map_to_legal_edges() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    cancelled = _item(row, orderNo="B-SEED", cancelled=row.quantity, balance=0, status="CS")
    assert [a.kind for a in plan_actions(row, 0, cancelled, now=_NOW)] == ["cancel_two_step"]
    expired = _item(row, orderNo="B-SEED", status="E")
    assert [a.kind for a in plan_actions(row, 0, expired, now=_NOW)] == ["expire"]


def test_post_ack_venue_reject_closes_via_cancel_path() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    item = _item(row, orderNo="B-SEED", rejectCode="7")
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["post_ack_reject"]
    assert actions[0].reason is not None and "7" in actions[0].reason


def test_pending_new_unmatched_waits_then_resolves_bounded() -> None:
    store = MemStore()
    young = _row(store, OrderState.PENDING_NEW, age_seconds=30)
    assert plan_actions(young, 0, None, now=_NOW) == []
    old = _row(store, OrderState.PENDING_NEW, age_seconds=61)
    actions = plan_actions(old, 0, None, now=_NOW)
    assert [a.kind for a in actions] == ["reject"]
    assert actions[0].reason == "ack_lost_unmatched"


def test_pending_cancel_confirms_or_waits() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_CANCEL)
    assert [a.kind for a in plan_actions(row, 0, None, now=_NOW)] == ["cancel_confirm"]
    confirmed = _item(row, orderNo="B-SEED", status="CS")
    assert [a.kind for a in plan_actions(row, 0, confirmed, now=_NOW)] == ["cancel_confirm"]
    still_live = _item(row, orderNo="B-SEED")
    assert plan_actions(row, 0, still_live, now=_NOW) == []


# ------------------------------------------------------- replace_resolve (NEW)


def test_pending_replace_resting_item_restores_venue_truth() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_REPLACE)
    item = _item(row, orderNo="B-SEED", price=101.5, vol=80, balance=80)
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["replace_resolve"]
    assert actions[0].replace_price == Decimal("101.5")
    assert actions[0].replace_qty == 80


def test_pending_replace_terminal_item_resolves_then_closes_out() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_REPLACE)
    item = _item(row, orderNo="B-SEED", status="CS", balance=0, cancelled=row.quantity)
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["replace_resolve", "cancel_two_step"]


def test_pending_replace_missing_item_restores_local_values() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_REPLACE)
    actions = plan_actions(row, 0, None, now=_NOW)
    assert [a.kind for a in actions] == ["replace_resolve"]
    assert actions[0].replace_price is None and actions[0].replace_qty is None


def test_fill_price_fallbacks() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    venue_priced = _item(row, orderNo="B-SEED", price=42.5)
    assert fill_price_for(row, venue_priced) == Decimal("42.500000")
    local = _item(row, orderNo="B-SEED", price=0)
    assert fill_price_for(row, local) == row.price


# ------------------------------------------------------------------ fuzzy match


def test_fuzzy_match_unique_and_ambiguous() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    ts = (row.created_at + timedelta(seconds=3)).isoformat()
    inside = _item(row, orderNo="7001", entryTime=ts)
    outside = _item(
        row, orderNo="7002", entryTime=(row.created_at + timedelta(seconds=9)).isoformat()
    )
    assert fuzzy_match(row, [inside, outside], claimed_order_nos=set()) is inside
    a = _item(row, orderNo="7001", entryTime=ts)
    b = _item(row, orderNo="7002", entryTime=ts)
    assert fuzzy_match(row, [a, b], claimed_order_nos=set()) is None  # ambiguous
    assert fuzzy_match(row, [a, b], claimed_order_nos={"7002"}) is a  # claimed excluded


# ------------------------------------------------------------- reconcile_once


def _reconciler() -> SettradeReconciler:
    return SettradeReconciler(
        make_adapter(),
        interval_seconds=12,
        pool_provider=lambda: object(),  # MemStore-patched repositories ignore the pool
        now=lambda: _NOW,
    )


def _set_orders_url(account: str) -> str:
    return f"{_BASE}/api/seos/v3/{_BROKER}/accounts/{account}/orders"


def _tfex_orders_url(account: str) -> str:
    return f"{_BASE}/api/seosd/v3/{_BROKER}/accounts/{account}/orders"


@respx.mock
async def test_full_lifecycle_driven_by_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """PENDING_NEW -> NEW -> PARTIALLY_FILLED -> FILLED across three passes."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    cid = row.client_order_id
    set_route = respx.get(_set_orders_url(row.account))
    respx.get(_tfex_orders_url(row.account)).respond(json=[])
    reconciler = _reconciler()

    set_route.respond(json=[_venue_json(row)])
    await reconciler.reconcile_once()
    assert store.orders[cid]["status"] is OrderState.NEW
    assert store.orders[cid]["broker_order_id"] == "SET-1"

    set_route.respond(json=[_venue_json(row, matched=40, balance=60)])
    await reconciler.reconcile_once()
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED
    assert [(f["broker_fill_id"], f["quantity"]) for f in store.fills[cid]] == [("SET-1:40", 40)]

    set_route.respond(json=[_venue_json(row, matched=100, balance=0)])
    await reconciler.reconcile_once()
    assert store.orders[cid]["status"] is OrderState.FILLED
    applied = await reconciler.reconcile_once()
    assert applied == 0  # idempotent re-poll


def _venue_json(row: OrderRow, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "orderNo": "SET-1",
        "accountNo": row.account,
        "symbol": row.symbol,
        "side": "Buy",
        "vol": row.quantity,
        "matched": 0,
        "balance": row.quantity,
        "cancelled": 0,
        "price": 100.0,
        "status": "O",
        "rejectCode": 0,
        "entryTime": (row.created_at + timedelta(seconds=2)).isoformat(),
    }
    base.update(overrides)
    return base


@respx.mock
async def test_per_account_market_grouping_fans_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two accounts x two markets -> four venue fetches."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    _row(store, OrderState.NEW, account="ACC-A", market="SET")
    _row(
        store, OrderState.NEW, account="ACC-A", market="TFEX", position_effect="OPEN", symbol="S50"
    )
    _row(store, OrderState.NEW, account="ACC-B", market="SET")
    _row(
        store, OrderState.NEW, account="ACC-B", market="TFEX", position_effect="OPEN", symbol="S50"
    )
    routes = {}
    for acct in ("ACC-A", "ACC-B"):
        routes[(acct, "SET")] = respx.get(_set_orders_url(acct)).respond(json=[])
        routes[(acct, "TFEX")] = respx.get(_tfex_orders_url(acct)).respond(json=[])
    await _reconciler().reconcile_once()
    assert all(r.called for r in routes.values())


@respx.mock
async def test_rate_budget_exhausted_skips_remaining_groups(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    _row(store, OrderState.NEW, account="ACC-A", market="SET")
    _row(store, OrderState.NEW, account="ACC-B", market="SET")
    reconciler = _reconciler()
    # Pre-exhaust the GET bucket: every group is skipped, no venue fetch fires.
    adapter = reconciler._adapter  # noqa: SLF001 - test hook
    adapter._client._rate["GET"] = RateBudget(remaining_second=0, remaining_minute=0)  # noqa: SLF001
    set_a = respx.get(_set_orders_url("ACC-A")).respond(json=[])
    set_b = respx.get(_set_orders_url("ACC-B")).respond(json=[])
    with caplog.at_level(logging.WARNING):
        applied = await reconciler.reconcile_once()
    assert applied == 0
    assert not set_a.called and not set_b.called
    assert "budget exhausted" in caplog.text


@respx.mock
async def test_replace_resolve_executor_restores_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stranded PENDING_REPLACE with prior fills resolves to PARTIALLY_FILLED."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    row = _row(store, OrderState.PENDING_REPLACE)
    cid = row.client_order_id
    # Seed a prior fill so the two-step restore (NEW -> PARTIALLY_FILLED) fires.
    store.fills[cid] = [
        {"broker_fill_id": "f1", "price": Decimal("100"), "quantity": 30, "exec_ts": _NOW}
    ]
    respx.get(_set_orders_url(row.account)).respond(
        json=[_venue_json(row, orderNo="B-SEED", matched=30, balance=70, price=101.0)]
    )
    respx.get(_tfex_orders_url(row.account)).respond(json=[])
    applied = await _reconciler().reconcile_once()
    assert applied >= 1
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED
    assert store.orders[cid]["price"] == Decimal("101.0")


@respx.mock
async def test_executor_drives_each_terminal_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """cancel_two_step / expire / reject / post_ack_reject through the executor."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    cancelled = _row(store, OrderState.NEW, symbol="AAA")
    expired = _row(store, OrderState.NEW, symbol="BBB")
    rejected = _row(store, OrderState.PENDING_NEW, age_seconds=10, symbol="CCC")
    post_ack = _row(store, OrderState.NEW, symbol="DDD")
    # Distinct venue order numbers so the venue index does not collide on B-SEED.
    for label, order in (("O-AAA", cancelled), ("O-BBB", expired), ("O-DDD", post_ack)):
        store.orders[order.client_order_id]["broker_order_id"] = label
    respx.get(_set_orders_url(cancelled.account)).respond(
        json=[
            _venue_json(cancelled, orderNo="O-AAA", symbol="AAA", status="CS", balance=0),
            _venue_json(expired, orderNo="O-BBB", symbol="BBB", status="E"),
            _venue_json(rejected, orderNo="R-1", symbol="CCC", rejectCode="9", rejectReason="bad"),
            _venue_json(post_ack, orderNo="O-DDD", symbol="DDD", rejectCode="7"),
        ]
    )
    respx.get(_tfex_orders_url(cancelled.account)).respond(json=[])
    await _reconciler().reconcile_once()
    assert store.orders[cancelled.client_order_id]["status"] is OrderState.CANCELLED
    assert store.orders[expired.client_order_id]["status"] is OrderState.EXPIRED
    assert store.orders[rejected.client_order_id]["status"] is OrderState.REJECTED
    assert store.orders[post_ack.client_order_id]["status"] is OrderState.CANCELLED


@respx.mock
async def test_group_venue_rejection_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx on one book's order list skips that group without dying."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    row = _row(store, OrderState.NEW)
    respx.get(_set_orders_url(row.account)).respond(
        status_code=404, json={"code": "1", "message": "no account"}
    )
    respx.get(_tfex_orders_url(row.account)).respond(json=[])
    applied = await _reconciler().reconcile_once()
    assert applied == 0
    assert store.orders[row.client_order_id]["status"] is OrderState.NEW


@respx.mock
async def test_resting_row_absent_from_book_is_drift_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    row = _row(store, OrderState.NEW, age_seconds=120)
    respx.get(_set_orders_url(row.account)).respond(json=[])
    respx.get(_tfex_orders_url(row.account)).respond(json=[])
    with caplog.at_level(logging.WARNING):
        applied = await _reconciler().reconcile_once()
    assert applied == 0
    assert store.orders[row.client_order_id]["status"] is OrderState.NEW
    assert "drift" in caplog.text


@respx.mock
async def test_empty_working_set_is_a_no_op_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    route = respx.get(_set_orders_url("ACC-TEST")).respond(json=[])
    applied = await _reconciler().reconcile_once()
    assert applied == 0
    assert not route.called


async def test_run_loop_cancels_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The background loop shell survives a pass and cancels cleanly."""
    import asyncio

    store = MemStore()
    patch_repositories(monkeypatch, store)
    reconciler = SettradeReconciler(
        make_adapter(), interval_seconds=0, pool_provider=lambda: object(), now=lambda: _NOW
    )
    task = asyncio.create_task(reconciler.run())
    await asyncio.sleep(0)  # let it spin at least once
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

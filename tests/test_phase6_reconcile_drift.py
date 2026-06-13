"""Phase 6 / C2 — reconciliation drift repair (DB truth, legal edges only).

Adversarial drift over the EXISTING reconcilers + repository seams (respx-mocked
venue, MemStore store):

* DB behind  — order NEW locally, FILLED at the venue → reconcile drives it through
               the frozen fill edge(s) to FILLED and records a fill row.
* DB ahead   — order FILLED locally (terminal), NEW at the venue → the reconciler's
               working set excludes terminals, so the store is NEVER regressed (no
               illegal downgrade is even attempted).
* replace_resolve (Settrade) — a stranded PENDING_REPLACE (ack lost mid-amend)
               resolves to NEW with venue-truth price/qty when the venue confirms,
               or restores the pre-replace local values when the venue row is gone;
               the resolution is logged either way and every edge stays legal.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from src.quant_execution_engine.adapters.liberator.reconciler import LiberatorReconciler
from src.quant_execution_engine.adapters.settrade.reconciler import SettradeReconciler
from src.quant_execution_engine.contracts.enums import Broker, OrderState
from src.quant_execution_engine.db.models import OrderRow

from tests._fakes import MemStore, patch_repositories
from tests.conftest import make_order
from tests.unit.adapters.liberator.test_adapter_place import _BASE as _LIB_BASE
from tests.unit.adapters.liberator.test_adapter_place import make_adapter as make_liberator_adapter
from tests.unit.adapters.settrade.test_adapter_place import (
    _BASE as _SET_BASE,
)
from tests.unit.adapters.settrade.test_adapter_place import (
    _BROKER,
    _login_route,
)
from tests.unit.adapters.settrade.test_adapter_place import (
    make_adapter as make_settrade_adapter,
)

_NOW = datetime(2026, 6, 13, 8, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------- liberator DB-behind


def _lib_row(store: MemStore, status: OrderState, **overrides: Any) -> OrderRow:
    order = make_order(broker="liberator", price="123.45", **overrides)
    store.seed(order, status)
    raw = store.orders[order.client_order_id]
    raw["created_at"] = _NOW - timedelta(seconds=10)
    return OrderRow(**raw)


def _lib_reconciler() -> LiberatorReconciler:
    return LiberatorReconciler(
        make_liberator_adapter(),
        interval_seconds=12,
        pool_provider=lambda: object(),
        now=lambda: _NOW,
    )


def _orders_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {"list": items}}}


def _lib_venue_json(row: OrderRow, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "orderNo": row.broker_order_id or "VEN-1",
        "accountNo": row.account,
        "symbol": row.symbol,
        "side": "B",
        "volume": row.quantity,
        "matched": 0,
        "balance": row.quantity,
        "cancelled": 0,
        "price": "123.45",
        "status": "PENDING",
        "statusShow": "O",
        "rejectCode": "",
    }
    base.update(overrides)
    return base


@respx.mock
async def test_db_behind_repairs_to_filled_and_records_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order NEW locally, fully matched at the venue → FILLED + a fill row."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _lib_row(store, OrderState.NEW)
    cid = row.client_order_id
    respx.get(f"{_LIB_BASE}/orders/{row.account}").respond(
        json=_orders_response(
            [_lib_venue_json(row, orderNo="B-SEED", matched=row.quantity, balance=0)]
        )
    )
    applied = await _lib_reconciler().reconcile_once()
    assert applied >= 1
    assert store.orders[cid]["status"] is OrderState.FILLED
    assert [(f["broker_fill_id"], f["quantity"]) for f in store.fills[cid]] == [
        (f"B-SEED:{row.quantity}", row.quantity)
    ]


@respx.mock
async def test_db_behind_partial_then_full_across_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEW → PARTIALLY_FILLED → FILLED via the cumulative matched watermark."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _lib_row(store, OrderState.NEW)
    cid = row.client_order_id
    route = respx.get(f"{_LIB_BASE}/orders/{row.account}")

    route.respond(json=_orders_response([_lib_venue_json(row, orderNo="B-SEED", matched=40)]))
    await _lib_reconciler().reconcile_once()
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED

    route.respond(
        json=_orders_response([_lib_venue_json(row, orderNo="B-SEED", matched=100, balance=0)])
    )
    await _lib_reconciler().reconcile_once()
    assert store.orders[cid]["status"] is OrderState.FILLED
    # The fill aggregate is the watermark deltas (40, then 60), never doubled.
    assert sum(f["quantity"] for f in store.fills[cid]) == 100


# --------------------------------------------------------------- DB-ahead (no regress)


@respx.mock
async def test_db_ahead_terminal_state_is_never_regressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order FILLED locally but still NEW at the venue → the loop does NOT downgrade.

    A terminal row is excluded from the reconcile working set, so the venue's stale
    NEW is ignored — no fetch result can drive an illegal FILLED→NEW transition.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _lib_row(store, OrderState.FILLED)
    cid = row.client_order_id
    store.orders[cid]["broker_order_id"] = "B-SEED"
    # The venue still shows it resting (NEW) — stale truth the loop must not honour.
    route = respx.get(f"{_LIB_BASE}/orders/{row.account}").respond(
        json=_orders_response([_lib_venue_json(row, orderNo="B-SEED", matched=0)])
    )

    applied = await _lib_reconciler().reconcile_once()

    assert applied == 0
    assert store.orders[cid]["status"] is OrderState.FILLED  # terminal stays terminal
    assert not route.called  # terminal rows are not even in the working set → no poll


@respx.mock
async def test_db_ahead_cancelled_state_is_never_regressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CANCELLED (terminal) order is likewise never reopened by stale venue truth."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _lib_row(store, OrderState.CANCELLED)
    cid = row.client_order_id
    store.orders[cid]["broker_order_id"] = "B-SEED"
    respx.get(f"{_LIB_BASE}/orders/{row.account}").respond(
        json=_orders_response([_lib_venue_json(row, orderNo="B-SEED", matched=0)])
    )
    applied = await _lib_reconciler().reconcile_once()
    assert applied == 0
    assert store.orders[cid]["status"] is OrderState.CANCELLED


# Confirm the structural guarantee directly: terminals are never fetched for reconcile.
async def test_terminal_rows_excluded_from_reconcile_working_set() -> None:
    store = MemStore()
    for state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED):
        order = make_order(broker="liberator", price="123.45", symbol=f"T-{state.value}")
        store.seed(order, state)
    rows = await store.fetch_orders_for_reconcile(object(), Broker.LIBERATOR)
    assert rows == []  # nothing terminal is ever reconciled


# ------------------------------------------------------- settrade replace_resolve


def _set_row(store: MemStore, status: OrderState, **overrides: Any) -> OrderRow:
    order = make_order(broker="settrade", price="100.00", account="ACC-TEST", **overrides)
    store.seed(order, status)
    raw = store.orders[order.client_order_id]
    raw["created_at"] = _NOW - timedelta(seconds=10)
    return OrderRow(**raw)


def _set_reconciler() -> SettradeReconciler:
    return SettradeReconciler(
        make_settrade_adapter(),
        interval_seconds=12,
        pool_provider=lambda: object(),
        now=lambda: _NOW,
    )


def _set_orders_url(account: str = "ACC-TEST") -> str:
    return f"{_SET_BASE}/api/seos/v3/{_BROKER}/accounts/{account}/orders"


def _tfex_orders_url(account: str = "ACC-TEST") -> str:
    return f"{_SET_BASE}/api/seosd/v3/{_BROKER}/accounts/{account}/orders"


def _set_venue_json(row: OrderRow, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "orderNo": row.broker_order_id or "SET-1",
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
    }
    base.update(overrides)
    return base


@respx.mock
async def test_replace_resolve_venue_confirms_restores_new_with_venue_truth(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stranded PENDING_REPLACE, venue resting with the new price/qty → NEW (truth)."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    row = _set_row(store, OrderState.PENDING_REPLACE)
    cid = row.client_order_id
    store.orders[cid]["broker_order_id"] = "B-SEED"
    respx.get(_set_orders_url()).respond(
        json=[_set_venue_json(row, orderNo="B-SEED", price=101.5, vol=80, balance=80)]
    )
    respx.get(_tfex_orders_url()).respond(json=[])

    with caplog.at_level(logging.INFO):
        applied = await _set_reconciler().reconcile_once()

    assert applied >= 1
    assert store.orders[cid]["status"] is OrderState.NEW  # the only legal PENDING_REPLACE exit
    assert store.orders[cid]["price"] == Decimal("101.5")  # venue-truth price
    assert store.orders[cid]["quantity"] == 80
    assert "PENDING_REPLACE resolved" in caplog.text


@respx.mock
async def test_replace_resolve_venue_missing_restores_local_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stranded PENDING_REPLACE, venue row gone → restore NEW with the local price/qty."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    row = _set_row(store, OrderState.PENDING_REPLACE)
    cid = row.client_order_id
    store.orders[cid]["broker_order_id"] = "B-SEED"
    original_price = store.orders[cid]["price"]
    original_qty = store.orders[cid]["quantity"]
    respx.get(_set_orders_url()).respond(json=[])  # the venue row is gone
    respx.get(_tfex_orders_url()).respond(json=[])

    with caplog.at_level(logging.INFO):
        applied = await _set_reconciler().reconcile_once()

    assert applied >= 1
    assert store.orders[cid]["status"] is OrderState.NEW
    # COALESCE keeps the pre-replace local values (None venue price/qty restores them).
    assert store.orders[cid]["price"] == original_price
    assert store.orders[cid]["quantity"] == original_qty
    assert "PENDING_REPLACE resolved" in caplog.text


@respx.mock
async def test_replace_resolve_with_prior_fills_restores_partially_filled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partly-filled stranded PENDING_REPLACE resolves to PARTIALLY_FILLED, legally."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    _login_route()
    row = _set_row(store, OrderState.PENDING_REPLACE)
    cid = row.client_order_id
    store.orders[cid]["broker_order_id"] = "B-SEED"
    store.fills[cid] = [
        {"broker_fill_id": "f1", "price": Decimal("100"), "quantity": 30, "exec_ts": _NOW}
    ]
    respx.get(_set_orders_url()).respond(
        json=[_set_venue_json(row, orderNo="B-SEED", matched=30, balance=70, price=101.0)]
    )
    respx.get(_tfex_orders_url()).respond(json=[])

    applied = await _set_reconciler().reconcile_once()

    assert applied >= 1
    # PENDING_REPLACE → NEW (replace_resolve) → PARTIALLY_FILLED (the two-step restore).
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED
    assert store.orders[cid]["price"] == Decimal("101.0")

"""Reconciler v1: plan_actions table + full passes over MemStore + respx venue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
from src.quant_execution_engine.adapters.liberator.errors import LiberatorTransportError
from src.quant_execution_engine.adapters.liberator.models import VenueOrderItem
from src.quant_execution_engine.adapters.liberator.reconciler import (
    LiberatorReconciler,
    fill_price_for,
    fuzzy_match,
    plan_actions,
)
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.db.models import OrderRow

from tests._fakes import MemStore, patch_repositories
from tests.conftest import make_order
from tests.unit.adapters.liberator.test_adapter_place import _BASE, make_adapter

_NOW = datetime(2026, 6, 11, 8, 0, 0, tzinfo=UTC)


def _row(
    store: MemStore, status: OrderState, *, age_seconds: float = 0.0, **overrides: Any
) -> OrderRow:
    order = make_order(broker="liberator", price="123.45", **overrides)
    store.seed(order, status)
    raw = store.orders[order.client_order_id]
    raw["created_at"] = _NOW - timedelta(seconds=age_seconds)
    return OrderRow(**raw)


def _item(row: OrderRow, **overrides: Any) -> VenueOrderItem:
    payload: dict[str, Any] = {
        "orderNo": row.broker_order_id or "3064",
        "accountNo": row.account,
        "symbol": row.symbol,
        "side": "B" if row.side.value == "BUY" else "S",
        "volume": row.quantity,
        "matched": 0,
        "balance": row.quantity,
        "cancelled": 0,
        "price": str(row.price or "0"),
        "status": "PENDING",
        "statusShow": "O",
        "rejectCode": "",
    }
    payload.update(overrides)
    return VenueOrderItem.model_validate(payload)


# ----------------------------------------------------------------- plan_actions


def test_pending_new_with_match_acks_then_carries_fills_and_terminals() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    item = _item(row, orderNo="3064", matched=40, cancelled=60, balance=0, status="CANCELLED")
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["ack", "fill", "cancel_two_step"]
    assert actions[0].broker_order_id == "3064"
    assert actions[1].fill_qty == 40
    assert actions[1].fill_id == "3064:40"


def test_pending_new_venue_reject_goes_straight_to_rejected_without_ack() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    item = _item(row, rejectCode="RJ-105")
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["reject"]
    assert actions[0].reason is not None and "RJ-105" in actions[0].reason


def test_pending_new_unmatched_waits_then_resolves_bounded() -> None:
    store = MemStore()
    young = _row(store, OrderState.PENDING_NEW, age_seconds=30)
    assert plan_actions(young, 0, None, now=_NOW) == []  # inside the window: wait
    old = _row(store, OrderState.PENDING_NEW, age_seconds=61)
    actions = plan_actions(old, 0, None, now=_NOW)
    assert [a.kind for a in actions] == ["reject"]
    assert actions[0].reason == "ack_lost_unmatched"


def test_fill_delta_is_cumulative_watermark() -> None:
    store = MemStore()
    row = _row(store, OrderState.PARTIALLY_FILLED)
    item = _item(row, orderNo="B-SEED", matched=70, balance=30)
    actions = plan_actions(row, 40, item, now=_NOW)
    assert [a.kind for a in actions] == ["fill"]
    assert actions[0].fill_qty == 30
    assert actions[0].fill_id == "B-SEED:70"
    assert actions[0].total_quantity == row.quantity
    # Re-poll with no progress: delta 0, no actions.
    assert plan_actions(row, 70, item, now=_NOW) == []


def test_full_match_emits_fill_without_terminal_actions() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    item = _item(row, orderNo="B-SEED", matched=row.quantity, balance=0, status="MATCHED")
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["fill"]  # apply_fill flips to FILLED itself


def test_venue_cancel_and_expire_map_to_legal_edges() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    cancelled = _item(row, orderNo="B-SEED", cancelled=row.quantity, balance=0, status="CANCELLED")
    assert [a.kind for a in plan_actions(row, 0, cancelled, now=_NOW)] == ["cancel_two_step"]
    expired = _item(row, orderNo="B-SEED", status="EXPIRED")
    assert [a.kind for a in plan_actions(row, 0, expired, now=_NOW)] == ["expire"]


def test_post_ack_venue_reject_closes_via_cancel_path() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    item = _item(row, orderNo="B-SEED", rejectCode="RJ-7")
    actions = plan_actions(row, 0, item, now=_NOW)
    assert [a.kind for a in actions] == ["post_ack_reject"]
    assert actions[0].reason is not None and "RJ-7" in actions[0].reason


def test_pending_cancel_confirms_on_venue_cancel_or_absence_waits_otherwise() -> None:
    store = MemStore()
    row = _row(store, OrderState.PENDING_CANCEL)
    gone = plan_actions(row, 0, None, now=_NOW)
    assert [a.kind for a in gone] == ["cancel_confirm"]
    confirmed = _item(row, orderNo="B-SEED", status="CANCELLED")
    assert [a.kind for a in plan_actions(row, 0, confirmed, now=_NOW)] == ["cancel_confirm"]
    still_live = _item(row, orderNo="B-SEED")
    assert plan_actions(row, 0, still_live, now=_NOW) == []
    late_fill = _item(row, orderNo="B-SEED", matched=10, balance=row.quantity - 10)
    assert plan_actions(row, 0, late_fill, now=_NOW) == []  # v1: surfaced, not persisted


def test_resting_row_absent_from_book_is_drift_warning_only() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW, age_seconds=120)
    assert plan_actions(row, 0, None, now=_NOW) == []


def test_fill_price_fallbacks() -> None:
    store = MemStore()
    row = _row(store, OrderState.NEW)
    venue_priced = _item(row, orderNo="B-SEED", price="42.50")
    assert fill_price_for(row, venue_priced) == Decimal("42.50")
    averaged = _item(row, orderNo="B-SEED", price="0", amount="4250", matched=100)
    assert fill_price_for(row, averaged) == Decimal("42.5")
    local = _item(row, orderNo="B-SEED", price="0", amount="0", matched=0)
    assert fill_price_for(row, local) == row.price


# ------------------------------------------------------------------ fuzzy match


def _fuzzy_setup() -> tuple[MemStore, OrderRow]:
    store = MemStore()
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    return store, row


def test_fuzzy_match_unique_within_window() -> None:
    _, row = _fuzzy_setup()
    ts_inside = (row.created_at + timedelta(seconds=3)).isoformat()
    ts_outside = (row.created_at + timedelta(seconds=9)).isoformat()
    inside = _item(row, orderNo="7001", entryTime=ts_inside)
    outside = _item(row, orderNo="7002", entryTime=ts_outside)
    wrong_qty = _item(row, orderNo="7003", volume=row.quantity + 1)
    assert fuzzy_match(row, [inside, outside, wrong_qty], claimed_order_nos=set()) is inside


def test_fuzzy_match_ambiguous_or_claimed_is_skipped() -> None:
    _, row = _fuzzy_setup()
    ts = (row.created_at + timedelta(seconds=1)).isoformat()
    a = _item(row, orderNo="7001", entryTime=ts)
    b = _item(row, orderNo="7002", entryTime=ts)
    assert fuzzy_match(row, [a, b], claimed_order_nos=set()) is None  # ambiguous: never guess
    assert fuzzy_match(row, [a, b], claimed_order_nos={"7002"}) is a  # claimed rows excluded
    wrong_side = _item(row, orderNo="7004", side="S", entryTime=ts)
    no_ts = _item(row, orderNo="7005")
    assert fuzzy_match(row, [wrong_side, no_ts], claimed_order_nos=set()) is None


# ------------------------------------------------------------- reconcile_once


def _reconciler(store: Any) -> LiberatorReconciler:
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
        "orderNo": "3064",
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
        "entryTime": (row.created_at + timedelta(seconds=2)).isoformat(),
    }
    base.update(overrides)
    return base


@respx.mock
async def test_required_case_5_full_lifecycle_driven_by_polls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PENDING_NEW -> NEW -> PARTIALLY_FILLED -> FILLED across three passes."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)
    cid = row.client_order_id
    route = respx.get(f"{_BASE}/orders/{row.account}")
    reconciler = _reconciler(store)

    route.respond(json=_orders_response([_venue_json(row)]))
    await reconciler.reconcile_once()
    assert store.orders[cid]["status"] is OrderState.NEW
    assert store.orders[cid]["broker_order_id"] == "3064"

    route.respond(json=_orders_response([_venue_json(row, matched=40, balance=60)]))
    await reconciler.reconcile_once()
    assert store.orders[cid]["status"] is OrderState.PARTIALLY_FILLED
    assert [(f["broker_fill_id"], f["quantity"]) for f in store.fills[cid]] == [("3064:40", 40)]

    route.respond(json=_orders_response([_venue_json(row, matched=100, balance=0)]))
    await reconciler.reconcile_once()
    assert store.orders[cid]["status"] is OrderState.FILLED
    assert [(f["broker_fill_id"], f["quantity"]) for f in store.fills[cid]] == [
        ("3064:40", 40),
        ("3064:100", 60),
    ]

    # Idempotent re-poll: no new fills, status unchanged.
    applied = await reconciler.reconcile_once()
    assert applied == 0
    assert len(store.fills[cid]) == 2


@respx.mock
async def test_required_case_6_lost_ack_fuzzy_match_advances_to_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=10)  # stuck > 5 s, no broker id
    assert store.orders[row.client_order_id]["broker_order_id"] is None
    respx.get(f"{_BASE}/orders/{row.account}").respond(
        json=_orders_response([_venue_json(row, orderNo="9-LOST")])
    )
    await _reconciler(store).reconcile_once()
    assert store.orders[row.client_order_id]["status"] is OrderState.NEW
    assert store.orders[row.client_order_id]["broker_order_id"] == "9-LOST"


@respx.mock
async def test_lost_ack_resolves_bounded_to_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=120)
    respx.get(f"{_BASE}/orders/{row.account}").respond(json=_orders_response([]))
    await _reconciler(store).reconcile_once()
    assert store.orders[row.client_order_id]["status"] is OrderState.REJECTED
    assert store.orders[row.client_order_id]["reject_reason"] == "ack_lost_unmatched"


@respx.mock
async def test_transport_failure_skips_account_without_dying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.NEW)
    respx.get(f"{_BASE}/orders/{row.account}").respond(status_code=503)
    applied = await _reconciler(store).reconcile_once()
    assert applied == 0
    assert store.orders[row.client_order_id]["status"] is OrderState.NEW


@respx.mock
async def test_empty_working_set_is_a_no_op_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    route = respx.get(f"{_BASE}/orders/ACC-TEST").respond(json=_orders_response([]))
    applied = await _reconciler(store).reconcile_once()
    assert applied == 0
    assert not route.called


# --------------------------------------------- resolve_order_now (TK-0423 burst)


@respx.mock
async def test_resolve_order_now_matches_a_row_TOO_YOUNG_for_the_steady_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 The gate bypass, proven by CONTRAST rather than asserted.

    The same row and the same venue snapshot are put through both entry points:
    ``reconcile_once`` must SKIP it (younger than ``_STUCK_PENDING_SECONDS``, so an
    ack could still be in flight) while ``resolve_order_now`` must MATCH it (it runs
    only after an ack that already returned with no handle).

    Asserting only that the burst matches would pass even if the gate had been
    deleted globally — which would let the steady loop fuzzy-match orders whose ack
    is still in flight. The skip is half the property.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=1)  # inside the 5 s gate
    cid = row.client_order_id
    respx.get(f"{_BASE}/orders/{row.account}").respond(
        json=_orders_response([_venue_json(row, orderNo="18439")])
    )
    reconciler = _reconciler(store)

    applied = await reconciler.reconcile_once()
    assert applied == 0, "the STEADY loop must still wait out the lost-ack window"
    assert store.orders[cid]["broker_order_id"] is None

    assert await reconciler.resolve_order_now(cid) is True
    assert store.orders[cid]["broker_order_id"] == "18439"
    assert store.orders[cid]["status"] is OrderState.NEW


@respx.mock
async def test_resolve_order_now_returns_False_when_the_venue_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Venue read fine, order not there -> False (the caller reports PENDING)."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=1)
    respx.get(f"{_BASE}/orders/{row.account}").respond(json=_orders_response([]))

    assert await _reconciler(store).resolve_order_now(row.client_order_id) is False
    assert store.orders[row.client_order_id]["broker_order_id"] is None


@respx.mock
async def test_resolve_order_now_RAISES_when_the_venue_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 An unreadable venue must RAISE, never return False.

    False means "the venue says it does not have it"; a transport failure means "we
    never asked". Collapsing them would let an outage be reported to the caller as
    PENDING — an order presented as safely working while nothing has been confirmed.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.PENDING_NEW, age_seconds=1)
    respx.get(f"{_BASE}/orders/{row.account}").respond(status_code=503)

    with pytest.raises(LiberatorTransportError):
        await _reconciler(store).resolve_order_now(row.client_order_id)


@respx.mock
async def test_resolve_order_now_short_circuits_an_already_resolved_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that already holds a handle needs no venue read at all.

    The route is left unmocked on purpose: if the implementation asked the venue
    anyway, respx would fail the request rather than let a wasted read pass silently.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    row = _row(store, OrderState.NEW)
    store.orders[row.client_order_id]["broker_order_id"] = "ALREADY-HELD"

    assert await _reconciler(store).resolve_order_now(row.client_order_id) is True


@respx.mock
async def test_resolve_order_now_will_not_steal_a_handle_owned_by_another_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only venue row is already bound to a DIFFERENT order, so no match is legal.

    Two economically identical orders are exactly what ``fuzzy_match`` cannot tell
    apart, so the claimed-handle exclusion is the thing standing between the burst
    and binding one venue order to two local rows.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    owner = _row(store, OrderState.NEW)
    store.orders[owner.client_order_id]["broker_order_id"] = "18439"
    twin = _row(store, OrderState.PENDING_NEW, age_seconds=1)
    respx.get(f"{_BASE}/orders/{twin.account}").respond(
        json=_orders_response([_venue_json(twin, orderNo="18439")])
    )

    assert await _reconciler(store).resolve_order_now(twin.client_order_id) is False
    assert store.orders[twin.client_order_id]["broker_order_id"] is None
    assert store.orders[owner.client_order_id]["broker_order_id"] == "18439"

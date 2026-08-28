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


# ------------------------------------------------- TK-0446: absence is not information


from datetime import UTC, datetime  # noqa: E402

from src.quant_execution_engine.adapters.liberator.reconciler import (  # noqa: E402
    absence_is_informative,
)


def _row_created(ts: datetime, status: OrderState = OrderState.PENDING_CANCEL) -> OrderRow:
    """An order row pinned to an exact creation instant.

    Mirrors this file's ``_row`` but sets ``created_at`` absolutely rather than as an
    offset from ``_NOW`` — these tests are about a calendar boundary, so the instant has
    to be stated, not derived.
    """
    store = MemStore()
    order = make_order(broker="liberator", price="123.45")
    store.seed(order, status)
    raw = store.orders[order.client_order_id]
    raw["created_at"] = ts
    return OrderRow(**raw)


class TestAbsenceAcrossTheVenueDayBoundary:
    """🔴 The venue book is CURRENT-DAY-ONLY, so absence expires as evidence.

    Within a day, "absent after a cancel request" means gone. From the next day EVERY
    order is absent by construction — so the same inference would confirm a terminal
    CANCELLED for an order that may still be resting at the venue.
    """

    def test_a_PENDING_CANCEL_absent_on_the_SAME_venue_day_still_confirms(self) -> None:
        """The existing behaviour must survive: this is not a blanket disabling."""
        now = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)  # 13:00 BKK
        row = _row_created(datetime(2026, 8, 28, 3, 0, tzinfo=UTC))  # 10:00 BKK, same day
        actions = plan_actions(row, 0, None, now=now)
        assert [a.kind for a in actions] == ["cancel_confirm"]

    def test_a_PENDING_CANCEL_absent_from_a_PREVIOUS_venue_day_confirms_NOTHING(self) -> None:
        """🔴 The fix. A terminal CANCELLED on no evidence is the failure being prevented."""
        now = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)  # 13:00 BKK, 28th
        row = _row_created(datetime(2026, 8, 27, 6, 0, tzinfo=UTC))  # 13:00 BKK, 27th
        assert plan_actions(row, 0, None, now=now) == []

    def test_the_boundary_is_BANGKOK_not_UTC(self) -> None:
        """🔑 The test that would fail a naive UTC-date comparison.

        18:00 UTC on the 27th is **01:00 BKK on the 28th** — already the venue's next day.
        A UTC-date check would call it "the 27th" and, against a `now` on the 28th, wrongly
        treat the row as stale and skip a confirm that is legitimately available.

        Both directions matter, so both are asserted: the venue day is what decides, and
        it does not coincide with the UTC day.
        """
        now = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)  # 13:00 BKK on the 28th

        # UTC 27th, but BKK 28th -> SAME venue day -> confirm is available.
        row_same = _row_created(datetime(2026, 8, 27, 18, 0, tzinfo=UTC))
        assert absence_is_informative(row_same, now) is True
        assert [a.kind for a in plan_actions(row_same, 0, None, now=now)] == ["cancel_confirm"]

        # UTC 28th 00:30, which is BKK 07:30 on the 28th -> also same day.
        row_early = _row_created(datetime(2026, 8, 28, 0, 30, tzinfo=UTC))
        assert absence_is_informative(row_early, now) is True

        # UTC 27th 16:00 = BKK 23:00 on the 27th -> PREVIOUS venue day.
        row_prev = _row_created(datetime(2026, 8, 27, 16, 0, tzinfo=UTC))
        assert absence_is_informative(row_prev, now) is False

    def test_a_NAIVE_created_at_is_read_as_UTC_not_as_local_time(self) -> None:
        """A naive timestamp must not shift the boundary by the host's timezone.

        ``astimezone()`` on a naive datetime assumes LOCAL time, which on this host (BKK)
        would move the boundary by 7 hours and silently change which rows are actionable.

        ⚠️ This exercises ``absence_is_informative`` **directly**, and that is deliberate:
        a naive ``created_at`` cannot actually reach it through ``plan_actions``, because
        the ``now - row.created_at`` age computation raises ``TypeError`` first. The
        column is ``timestamptz`` so production rows are aware. The normalisation is
        therefore belt-and-braces for direct callers, and this test says so rather than
        implying a path that does not exist.
        """
        now = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
        naive_same = _row_created(datetime(2026, 8, 27, 18, 0))  # UTC -> BKK 28th
        assert absence_is_informative(naive_same, now) is True
        naive_prev = _row_created(datetime(2026, 8, 27, 6, 0))  # UTC -> BKK 27th
        assert absence_is_informative(naive_prev, now) is False

    def test_PENDING_NEW_is_UNAFFECTED_because_it_resolves_same_day(self) -> None:
        """Scope control: the guard must not silence the lost-ack path.

        PENDING_NEW resolves inside a 60 s window, so it can never reach a day boundary —
        and if this guard leaked into it, a genuinely lost ack would stop being rejected.
        """
        now = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)
        # Aware, because that is what production rows are (timestamptz) and what
        # plan_actions requires — see the naive-timestamp note above.
        row = _row_created(datetime(2026, 8, 27, 6, 0, tzinfo=UTC), status=OrderState.PENDING_NEW)
        actions = plan_actions(row, 0, None, now=now)
        assert [a.kind for a in actions] == ["reject"]
        assert actions[0].reason == "ack_lost_unmatched"

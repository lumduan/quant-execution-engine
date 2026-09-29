"""Order history for one account (GH #398, operator ruling 2026-09-29).

Two routes, two different questions, and each response must SAY what it cannot contain:

* ``/accounts/{a}/orders`` — the engine's store. Complete only for what THIS ENGINE routed.
* ``/accounts/{a}/venue-orders`` — the venue's own list. Every state and origin, only as far back as
  the venue keeps (Liberator: today, OBSERVED; Streaming Pro: NOT ESTABLISHED).

The display-state rule is tested at the boundary that matters: a fully matched order must read
"filled", although the reconciler's coarse classifier calls it "resting".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
    VenueDisplayState,
    VenueOrderView,
    display_state,
)
from src.quant_execution_engine.adapters.liberator.mapping import venue_item_to_view
from src.quant_execution_engine.adapters.liberator.models import VenueOrderItem
from src.quant_execution_engine.adapters.streaming_pro.mapping import venue_row_to_view
from src.quant_execution_engine.adapters.streaming_pro.models import VenueOrderRow
from src.quant_execution_engine.api import deps
from src.quant_execution_engine.api.main import create_app
from src.quant_execution_engine.contracts.enums import Broker, Market, Side
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core.router import OrderRouter

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import build_client, make_settings, order_payload

_ACCT = "70000002"


# --------------------------------------------------------------- display state (unit)


@pytest.mark.parametrize(
    ("classified", "qty", "matched", "remaining", "want"),
    [
        ("resting", 1000, 1000, 0, VenueDisplayState.FILLED),  # the case the classifier gets wrong
        ("resting", 10, 4, 6, VenueDisplayState.PARTIALLY_FILLED),
        ("resting", 5, 0, 5, VenueDisplayState.RESTING),
        ("resting", 5, 0, 0, VenueDisplayState.UNKNOWN),  # cannot tell -> say so, never guess
        ("cancelled", 10, 4, 0, VenueDisplayState.CANCELLED),  # terminal word wins
        ("rejected", 1, 0, 0, VenueDisplayState.REJECTED),
        ("expired", 1, 0, 1, VenueDisplayState.EXPIRED),
    ],
)
def test_display_state(classified: str, qty: int, matched: int, remaining: int, want: Any) -> None:
    assert display_state(classified, quantity=qty, matched=matched, remaining=remaining) is want


# --------------------------------------------------------------- venue row -> view (unit)


def _lib(**kw: Any) -> VenueOrderItem:
    base = {
        "orderNo": "18197",
        "symbol": "BGRIM",
        "side": "B",
        "volume": 1000,
        "matched": 0,
        "balance": 1000,
        "priceType": "Limit",
        "price": "19.90",
        "status": "",
        "statusShow": "",
    }
    return VenueOrderItem.model_validate({**base, **kw})


def test_liberator_matched_row_is_FILLED_and_keeps_the_venues_own_words() -> None:
    view = venue_item_to_view(
        _lib(matched=1000, balance=0, statusShow="M", entryTime="2026-09-29T10:15:00+07:00")
    )
    assert view.state is VenueDisplayState.FILLED
    assert view.venue_status_code == "M"  # verbatim, so a reader never depends on our mapping
    assert view.market is Market.SET and view.side is Side.BUY
    assert view.placed_at is not None and view.placed_at.startswith("2026-09-29T10:15")


@pytest.mark.parametrize(
    ("show", "want"), [("X", "cancelled"), ("C", "cancelled"), ("XC", "cancelled")]
)
def test_liberator_cancel_codes(show: str, want: str) -> None:
    assert venue_item_to_view(_lib(statusShow=show, balance=0)).state.value == want


def test_liberator_row_with_an_unmappable_side_is_KEPT_not_dropped() -> None:
    view = venue_item_to_view(_lib(side="?"))
    assert view.side is None and view.venue_order_id == "18197"


def test_liberator_tfex_row_is_tfex() -> None:
    assert venue_item_to_view(_lib(position="Open", side="S")).market is Market.TFEX


def test_streaming_pro_row_carries_the_front_it_was_read_from() -> None:
    row = VenueOrderRow.model_validate(
        {
            "orderNo": "SP-SYNTH-1",
            "symbol": "PTT",
            "side": "Sell",
            "qty": 100,
            "matchQty": 40,
            "balanceQty": 60,
            "status": "Open",
            "price": "35.00",
        }
    )
    view = venue_row_to_view(row, market=Market.TFEX)
    assert view.market is Market.TFEX and view.state is VenueDisplayState.PARTIALLY_FILLED


# --------------------------------------------------------------- the store route


def _sim_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, MemStore]:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    client, _ = build_client(
        settings=make_settings(public_mode=False), pool=object(), redis=FakeRedis()
    )
    return client, store


def test_store_route_returns_only_this_accounts_orders_with_fills_and_says_what_it_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _sim_client(monkeypatch)
    mine = order_payload(account="ACCT-A")
    assert client.post("/orders", json=mine).status_code == 201
    assert client.post("/orders", json=order_payload(account="ACCT-B")).status_code == 201

    body = client.get("/accounts/ACCT-A/orders?broker=sim").json()
    assert [o["client_order_id"] for o in body["orders"]] == [mine["client_order_id"]]
    order = body["orders"][0]
    assert order["broker"] == "sim" and "public_status" in order and "fills" in order
    cov = body["coverage"]
    assert cov["source"] == "engine_store"
    assert any("by hand" in c for c in cov["cannot_contain"])
    assert cov["complete_since"] is not None


def test_store_route_paginates_without_skipping_or_repeating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = _sim_client(monkeypatch)
    sent = []
    for i in range(5):
        p = order_payload(account="ACCT-P", symbol=f"S{i}")
        assert client.post("/orders", json=p).status_code == 201
        sent.append(p["client_order_id"])
    # Force identical timestamps: the keyset must still separate them by client_order_id.
    same = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
    for cid in sent:
        store.orders[cid]["created_at"] = same

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        q = "/accounts/ACCT-P/orders?broker=sim&limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(q).json()
        seen += [o["client_order_id"] for o in body["orders"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(sent) and len(seen) == len(set(seen)) == 5


def test_store_route_window_excludes_orders_outside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = _sim_client(monkeypatch)
    p = order_payload(account="ACCT-W")
    assert client.post("/orders", json=p).status_code == 201
    store.orders[p["client_order_id"]]["created_at"] = datetime(2026, 1, 1, tzinfo=UTC)
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    assert client.get(f"/accounts/ACCT-W/orders?broker=sim&since={since}").json()["orders"] == []


@pytest.mark.parametrize("q", ["since=2026-09-29T00:00:00", "until=2026-09-29T00:00:00"])
def test_a_naive_timestamp_is_refused_never_assumed_utc(
    monkeypatch: pytest.MonkeyPatch, q: str
) -> None:
    client, _ = _sim_client(monkeypatch)
    assert client.get(f"/accounts/ACCT/orders?broker=sim&{q}").status_code == 422


def test_a_malformed_cursor_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _sim_client(monkeypatch)
    assert client.get("/accounts/ACCT/orders?broker=sim&cursor=garbage").status_code == 422


def test_store_route_is_gated_like_every_other_account_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paper + a real broker with no runtime: the SAME 403 the other account reads give."""
    patch_repositories(monkeypatch, MemStore())
    client, _ = build_client(
        settings=make_settings(public_mode=False, stage="paper"), pool=object(), redis=FakeRedis()
    )
    for path in ("orders", "venue-orders"):
        resp = client.get(f"/accounts/{_ACCT}/{path}?broker=liberator")
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "stage_rejected", path


# --------------------------------------------------------------- the venue route


class _VenueAdapter(BrokerAdapter):
    def __init__(self, views: list[VenueOrderView]) -> None:
        super().__init__()
        self.broker = Broker.LIBERATOR  # type: ignore[misc]
        self._views = views

    async def get_venue_orders(self, account: str) -> list[VenueOrderView]:
        return self._views

    async def amend(  # pragma: no cover - unused
        self, client_order_id: str, new_price: Decimal | None = None, new_qty: int | None = None
    ) -> AmendAck:
        raise AssertionError

    async def place(self, order: NormalizedOrder) -> PlaceAck:  # pragma: no cover - unused
        raise AssertionError

    async def cancel(self, client_order_id: str) -> CancelAck:  # pragma: no cover - unused
        raise AssertionError

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:  # pragma: no cover
        return []

    async def get_positions(self, account: str) -> list[Position]:  # pragma: no cover
        return []

    async def get_account(self, account: str) -> AccountInfo:  # pragma: no cover
        raise AssertionError

    def capabilities(self) -> tuple[Any, ...]:  # pragma: no cover
        return ()


def _live_client(
    monkeypatch: pytest.MonkeyPatch, adapter: BrokerAdapter, declared: list[str]
) -> TestClient:
    patch_repositories(monkeypatch, MemStore())
    settings = make_settings(public_mode=False, stage="micro_live", real_routing_accounts=declared)
    router = OrderRouter(
        settings=settings, pool=object(), redis=FakeRedis(), liberator_adapter=adapter
    )
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_pool_dep] = lambda: object()
    app.dependency_overrides[deps.get_redis_dep] = lambda: FakeRedis()
    app.dependency_overrides[deps.get_router_dep] = lambda: router
    return TestClient(app, headers={"X-API-Key": settings.api_key or ""})


def _view(**kw: Any) -> VenueOrderView:
    base: dict[str, Any] = dict(
        venue_order_id="1",
        market=Market.SET,
        symbol="PTT",
        side=Side.BUY,
        quantity=100,
        price=Decimal("35"),
        matched_qty=100,
        remaining_qty=0,
        cancelled_qty=0,
        state=VenueDisplayState.FILLED,
        venue_status="Matched",
        venue_status_code="M",
    )
    return VenueOrderView(**{**base, **kw})


def test_venue_route_returns_every_row_and_states_its_day_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    views = [
        _view(),
        _view(venue_order_id="2", state=VenueDisplayState.CANCELLED, venue_status_code="X"),
    ]
    body = (
        _live_client(monkeypatch, _VenueAdapter(views), [_ACCT])
        .get(f"/accounts/{_ACCT}/venue-orders?broker=liberator")
        .json()
    )
    assert [o["venue_order_id"] for o in body["orders"]] == ["1", "2"]
    cov = body["coverage"]
    assert cov["day_scope"] == "today_only" and cov["day_scope_evidence"].startswith("OBSERVED")
    assert cov["cannot_contain"] == ["any order from a previous trading day"]


def test_venue_route_is_refused_for_an_undeclared_account_EH6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = _live_client(monkeypatch, _VenueAdapter([]), ["70000007"]).get(
        f"/accounts/{_ACCT}/venue-orders?broker=liberator"
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "real_routing_not_authorized"


def test_streaming_pro_day_scope_is_NOT_claimed() -> None:
    from src.quant_execution_engine.api.routes import _VENUE_SCOPE

    scope = _VENUE_SCOPE[Broker.STREAMING_PRO]
    assert scope["day_scope"] == "not_established"
    assert str(scope["day_scope_evidence"]).startswith("NOT MEASURED")


def test_an_adapter_without_a_venue_list_answers_501_not_an_empty_list() -> None:
    import asyncio

    class _Bare(_VenueAdapter):
        get_venue_orders = BrokerAdapter.get_venue_orders

    with pytest.raises(Exception) as exc:
        asyncio.run(_Bare([]).get_venue_orders(_ACCT))
    assert getattr(exc.value, "code", None) == "venue_orders_unavailable"

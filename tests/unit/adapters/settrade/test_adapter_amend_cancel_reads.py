"""SettradeAdapter native amend, cancel (cache + resolver), reads, account chain."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import respx
from src.quant_execution_engine.adapters.settrade.adapter import (
    SettradeOrderIdResolver,
)
from src.quant_execution_engine.contracts.enums import Market, OrderType, Side

from tests.unit.adapters.settrade.test_adapter_place import (
    _ACCOUNT,
    _BASE,
    _BROKER,
    _login_route,
    make_adapter,
)


def _resolver(order_no: str, market: Market, account: str = _ACCOUNT) -> SettradeOrderIdResolver:
    async def resolve(client_order_id: str) -> tuple[str, Market, str] | None:
        return (order_no, market, account)

    return resolve


def _set_order_url(order_no: str, action: str) -> str:
    return f"{_BASE}/api/seos/v3/{_BROKER}/accounts/{_ACCOUNT}/orders/{order_no}/{action}"


def _tfex_order_url(order_no: str, action: str) -> str:
    return f"{_BASE}/api/seosd/v3/{_BROKER}/accounts/{_ACCOUNT}/orders/{order_no}/{action}"


def _set_orders_url(account: str = _ACCOUNT) -> str:
    return f"{_BASE}/api/seos/v3/{_BROKER}/accounts/{account}/orders"


def _tfex_orders_url(account: str = _ACCOUNT) -> str:
    return f"{_BASE}/api/seosd/v3/{_BROKER}/accounts/{account}/orders"


def _portfolios_url(book: str, account: str = _ACCOUNT) -> str:
    return f"{_BASE}/api/{book}/v3/{_BROKER}/accounts/{account}/portfolios"


def _account_info_url(book: str, account: str = _ACCOUNT) -> str:
    return f"{_BASE}/api/{book}/v3/{_BROKER}/accounts/{account}/account-info"


# ------------------------------------------------------------------- amend


@respx.mock
async def test_amend_native_ok_on_empty_body() -> None:
    _login_route()
    route = respx.patch(_set_order_url("SET-1", "change")).respond(json={})
    adapter = make_adapter(resolve_order=_resolver("SET-1", Market.SET))
    ack = await adapter.amend("cid", new_price=Decimal("101.00"))
    assert ack.ok
    assert ack.semantics == "native"
    assert route.called
    await adapter.aclose()


@respx.mock
async def test_amend_venue_reject_is_not_ok_with_venue_text() -> None:
    _login_route()
    respx.patch(_set_order_url("SET-1", "change")).respond(
        status_code=409, json={"code": "2200", "message": "order already matched"}
    )
    adapter = make_adapter(resolve_order=_resolver("SET-1", Market.SET))
    ack = await adapter.amend("cid", new_price=Decimal("101.00"))
    assert not ack.ok
    assert ack.semantics == "native"
    assert ack.reason is not None and "order already matched" in ack.reason
    await adapter.aclose()


@respx.mock
async def test_amend_transport_failure_is_not_ok_never_raises() -> None:
    _login_route()
    respx.patch(_set_order_url("SET-1", "change")).respond(status_code=503)
    adapter = make_adapter(resolve_order=_resolver("SET-1", Market.SET))
    ack = await adapter.amend("cid", new_price=Decimal("101.00"))
    assert not ack.ok and ack.reason is not None
    await adapter.aclose()


async def test_amend_unknown_order_is_not_ok_without_resolver() -> None:
    adapter = make_adapter()  # no resolver, empty cache
    ack = await adapter.amend("missing", new_price=Decimal("1"))
    assert not ack.ok
    assert ack.reason is not None and "unknown broker order id" in ack.reason
    await adapter.aclose()


@respx.mock
async def test_amend_mapping_error_is_not_ok() -> None:
    _login_route()
    route = respx.patch(_set_order_url("SET-1", "change")).respond(json={})
    adapter = make_adapter(resolve_order=_resolver("SET-1", Market.SET))
    ack = await adapter.amend("cid")  # neither new_price nor new_qty
    assert not ack.ok and ack.reason is not None and "mapping" in ack.reason
    assert not route.called
    await adapter.aclose()


# ------------------------------------------------------------------- cancel


@respx.mock
async def test_cancel_ok_via_resolver_fallback() -> None:
    _login_route()
    route = respx.patch(_set_order_url("SET-2", "cancel")).respond(json={})
    adapter = make_adapter(resolve_order=_resolver("SET-2", Market.SET))
    ack = await adapter.cancel("cid")
    assert ack.ok and route.called
    await adapter.aclose()


@respx.mock
async def test_cancel_venue_reject_is_not_ok() -> None:
    _login_route()
    respx.patch(_set_order_url("SET-2", "cancel")).respond(
        status_code=409, json={"code": "3000", "message": "too late to cancel"}
    )
    adapter = make_adapter(resolve_order=_resolver("SET-2", Market.SET))
    ack = await adapter.cancel("cid")
    assert not ack.ok and ack.reason is not None and "too late to cancel" in ack.reason
    await adapter.aclose()


@respx.mock
async def test_cancel_transport_failure_is_not_ok() -> None:
    _login_route()
    respx.patch(_set_order_url("SET-2", "cancel")).respond(status_code=502)
    adapter = make_adapter(resolve_order=_resolver("SET-2", Market.SET))
    ack = await adapter.cancel("cid")
    assert not ack.ok and ack.reason is not None
    await adapter.aclose()


async def test_cancel_unknown_order_is_not_ok() -> None:
    adapter = make_adapter()
    ack = await adapter.cancel("missing")
    assert not ack.ok and ack.reason is not None and "unknown broker order id" in ack.reason
    await adapter.aclose()


@respx.mock
async def test_tfex_amend_order_no_on_wire_is_numeric_url() -> None:
    """TFEX order numbers ride the URL as the numeric string (assert path)."""
    _login_route()
    route = respx.patch(_tfex_order_url("9001", "change")).respond(json={})
    adapter = make_adapter(resolve_order=_resolver("9001", Market.TFEX))
    ack = await adapter.amend("cid", new_qty=5)
    assert ack.ok
    assert route.called
    assert route.calls.last.request.url.path.endswith("/orders/9001/change")
    await adapter.aclose()


# ------------------------------------------------------------------- reads


def _set_item(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "orderNo": "SET-1",
        "accountNo": _ACCOUNT,
        "symbol": "PTT",
        "side": "Buy",
        "priceType": "Limit",
        "vol": 100,
        "matched": 0,
        "balance": 100,
        "cancelled": 0,
        "price": 33.5,
        "status": "O",
        "validity": "Day",
        "rejectCode": 0,
    }
    base.update(overrides)
    return base


@respx.mock
async def test_get_open_orders_merges_both_markets_skips_unmappable() -> None:
    _login_route()
    respx.get(_set_orders_url()).respond(
        json=[
            _set_item(orderNo="SET-1"),  # representable open SET limit buy
            _set_item(orderNo="SET-2", balance=0, status="CS"),  # cancelled, not open
            _set_item(orderNo="SET-3", priceType="WEIRD"),  # unmappable price type
        ]
    )
    respx.get(_tfex_orders_url()).respond(
        json=[
            {
                "orderNo": 9001,
                "accountNo": _ACCOUNT,
                "symbol": "S50H26",
                "side": "Long",
                "position": "Open",
                "priceType": "Limit",
                "qty": 2,
                "matched": 0,
                "balance": 2,
                "price": 950.0,
                "status": "O",
                "validity": "Day",
                "rejectCode": 0,
            }
        ]
    )
    adapter = make_adapter()
    views = await adapter.get_open_orders(_ACCOUNT)
    assert {v.symbol for v in views} == {"PTT", "S50H26"}
    set_view = next(v for v in views if v.symbol == "PTT")
    assert set_view.side is Side.BUY
    assert set_view.order_type is OrderType.LIMIT
    assert set_view.price == Decimal("33.5")
    await adapter.aclose()


@respx.mock
async def test_get_open_orders_tolerates_one_book_rejecting() -> None:
    _login_route()
    respx.get(_set_orders_url()).respond(json=[_set_item()])
    respx.get(_tfex_orders_url()).respond(
        status_code=403, json={"code": "9", "message": "no derivatives account"}
    )
    adapter = make_adapter()
    views = await adapter.get_open_orders(_ACCOUNT)
    assert [v.symbol for v in views] == ["PTT"]
    await adapter.aclose()


@respx.mock
async def test_get_positions_merges_both_books() -> None:
    _login_route()
    respx.get(_portfolios_url("seos")).respond(json=[{"symbol": "PTT", "currentVolume": 300}])
    respx.get(_portfolios_url("seosd")).respond(
        json={"data": [{"symbol": "S50H26", "actualLongPosition": 5, "actualShortPosition": 2}]}
    )
    adapter = make_adapter()
    positions = await adapter.get_positions(_ACCOUNT)
    by_symbol = {(p.symbol, p.market): p.net_qty for p in positions}
    assert by_symbol[("PTT", Market.SET)] == 300
    assert by_symbol[("S50H26", Market.TFEX)] == 3
    await adapter.aclose()


@respx.mock
async def test_get_account_equity_first_buying_power_chain() -> None:
    _login_route()
    respx.get(_account_info_url("seos")).respond(
        json={"data": {"lineAvailable": 125000.5, "excessEquity": 50000}}
    )
    adapter = make_adapter()
    account = await adapter.get_account(_ACCOUNT)
    assert account.buying_power == Decimal("125000.5")  # line_available wins
    await adapter.aclose()


@respx.mock
async def test_get_account_falls_through_to_derivatives_then_zero() -> None:
    _login_route()
    respx.get(_account_info_url("seos")).respond(
        status_code=404, json={"code": "1", "message": "no equity account"}
    )
    respx.get(_account_info_url("seosd")).respond(json={"data": {"cashBalance": 9000}})
    adapter = make_adapter()
    account = await adapter.get_account(_ACCOUNT)
    assert account.buying_power == Decimal("9000")  # cash_balance fallback
    await adapter.aclose()


@respx.mock
async def test_get_account_zero_when_both_books_reject() -> None:
    _login_route()
    respx.get(_account_info_url("seos")).respond(
        status_code=404, json={"code": "1", "message": "no equity"}
    )
    respx.get(_account_info_url("seosd")).respond(
        status_code=404, json={"code": "1", "message": "no deriv"}
    )
    adapter = make_adapter()
    account = await adapter.get_account(_ACCOUNT)
    assert account.buying_power == Decimal("0")
    await adapter.aclose()


@respx.mock
async def test_get_account_zero_when_bodies_carry_no_money() -> None:
    """A non-dict body and a money-free body fall through to zero."""
    _login_route()
    respx.get(_account_info_url("seos")).respond(json=[])  # non-dict body skipped
    respx.get(_account_info_url("seosd")).respond(json={"data": {"unrelated": "x"}})
    adapter = make_adapter()
    account = await adapter.get_account(_ACCOUNT)
    assert account.buying_power == Decimal("0")
    await adapter.aclose()


@respx.mock
async def test_get_open_orders_skips_non_resting_rows() -> None:
    """A row classified as cancelled (balance>0 but a cancel status) is skipped."""
    _login_route()
    respx.get(_set_orders_url()).respond(
        json=[_set_item(orderNo="SET-9", status="cancelled", balance=50)]
    )
    respx.get(_tfex_orders_url()).respond(json=[])
    adapter = make_adapter()
    views = await adapter.get_open_orders(_ACCOUNT)
    assert views == []
    await adapter.aclose()


@respx.mock
async def test_get_positions_tolerates_garbage_and_wrappers() -> None:
    """Non-dict rows and a bare/garbage portfolio body never crash the read."""
    _login_route()
    respx.get(_portfolios_url("seos")).respond(
        json=[{"symbol": "PTT", "currentVolume": 100}, "garbage", 42]
    )
    respx.get(_portfolios_url("seosd")).respond(json={"unexpected": "shape"})  # not a list
    adapter = make_adapter()
    positions = await adapter.get_positions(_ACCOUNT)
    assert [(p.symbol, p.net_qty) for p in positions] == [("PTT", 100)]
    await adapter.aclose()


@respx.mock
async def test_place_2xx_non_rejected_order_object_acks() -> None:
    """A 2xx full order object that is NOT rejected still acks on its order_no."""
    from tests.unit.adapters.settrade.test_adapter_place import _settrade_order
    from tests.unit.adapters.settrade.test_adapter_place import make_adapter as _ma

    _login_route()
    respx.post(_set_orders_url()).respond(
        json={"orderNo": "SET-50", "rejectCode": 0, "rejectReason": None, "status": "O"}
    )
    adapter = _ma()
    ack = await adapter.place(_settrade_order())
    assert not ack.rejected and ack.broker_order_id == "SET-50"
    await adapter.aclose()

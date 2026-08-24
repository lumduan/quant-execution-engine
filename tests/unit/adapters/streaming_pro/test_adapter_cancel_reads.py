"""StreamingProAdapter cancel (per-market) + reads (open orders / positions / account / amend)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
import respx
from src.quant_execution_engine.adapters.base import AccountType
from src.quant_execution_engine.adapters.streaming_pro.errors import (
    StreamingProAccountUnavailable,
)
from src.quant_execution_engine.contracts.enums import Market

from tests.unit.adapters.streaming_pro.test_adapter_place import _BASE, make_adapter


async def test_cancel_no_mapping_returns_not_ok() -> None:
    adapter = make_adapter()
    ack = await adapter.cancel("unknown-cid")
    assert not ack.ok and ack.reason is not None and "no broker_order_id" in ack.reason
    await adapter.aclose()


@respx.mock
async def test_cancel_tfex_sends_bare_order_no() -> None:
    route = respx.post(f"{_BASE}/order/cancel").respond(json={"ok": True, "results": []})
    adapter = make_adapter()
    adapter._order_ref_cache["cid-1"] = ("8962991", Market.TFEX, "ACC", "USDM26")
    ack = await adapter.cancel("cid-1")
    assert ack.ok
    body = json.loads(route.calls.last.request.content)
    assert body == {"order_no": "8962991", "market": "TFEX", "account": "ACC"}
    await adapter.aclose()


@respx.mock
async def test_cancel_set_resolves_ext_order_no_via_orders() -> None:
    respx.get(f"{_BASE}/orders", params={"account": "ACC", "market": "SET"}).respond(
        json=[{"orderNo": "71937953", "extOrderNo": "0000031750", "symbol": "A"}]
    )
    route = respx.post(f"{_BASE}/order/cancel").respond(json={"ok": True})
    adapter = make_adapter()
    adapter._order_ref_cache["cid-2"] = ("71937953", Market.SET, "ACC", "A")
    ack = await adapter.cancel("cid-2")
    assert ack.ok
    body = json.loads(route.calls.last.request.content)
    assert body["ext_order_no"] == "0000031750" and body["symbol"] == "A"
    await adapter.aclose()


@respx.mock
async def test_cancel_uses_injected_resolver_when_not_cached() -> None:
    route = respx.post(f"{_BASE}/order/cancel").respond(json={"ok": True})

    async def _resolve(cid: str) -> tuple[str, Market, str, str] | None:
        return ("999", Market.TFEX, "ACC", "USDM26") if cid == "cid-r" else None

    adapter = make_adapter(resolve_order=_resolve)
    assert (await adapter.cancel("cid-r")).ok
    assert route.called
    await adapter.aclose()


@respx.mock
async def test_cancel_venue_reject_is_not_ok() -> None:
    respx.post(f"{_BASE}/order/cancel").respond(json={"ok": False, "reject_reason": "too late"})
    adapter = make_adapter()
    adapter._order_ref_cache["cid-3"] = ("1", Market.TFEX, "ACC", "USDM26")
    ack = await adapter.cancel("cid-3")
    assert not ack.ok and ack.reason is not None and "too late" in ack.reason
    await adapter.aclose()


@respx.mock
async def test_cancel_transport_failure_keeps_pending() -> None:
    respx.post(f"{_BASE}/order/cancel").respond(status_code=503)
    adapter = make_adapter()
    adapter._order_ref_cache["cid-4"] = ("1", Market.TFEX, "ACC", "USDM26")
    ack = await adapter.cancel("cid-4")
    assert not ack.ok  # router keeps PENDING_CANCEL; the reconciler resolves it
    await adapter.aclose()


@respx.mock
async def test_get_open_orders_normalizes_resting_rows_both_markets() -> None:
    respx.get(f"{_BASE}/orders", params={"account": "ACC", "market": "SET"}).respond(
        json=[
            {
                "orderNo": "1",
                "symbol": "PTT",
                "side": "Buy",
                "priceType": "Limit",
                "price": "35.00",
                "balanceQty": 100,
                "qty": 100,
                "status": "S",
            },
            {
                "orderNo": "2",
                "symbol": "AOT",
                "side": "Buy",
                "priceType": "Limit",
                "price": "60.00",
                "balanceQty": 0,
                "qty": 100,
                "status": "S",
            },  # fully matched -> skipped
        ]
    )
    respx.get(f"{_BASE}/orders", params={"account": "ACC", "market": "TFEX"}).respond(json=[])
    adapter = make_adapter()
    orders = await adapter.get_open_orders("ACC")
    assert [o.symbol for o in orders] == ["PTT"]
    await adapter.aclose()


@respx.mock
async def test_get_positions_maps_portfolio() -> None:
    respx.get(f"{_BASE}/portfolio", params={"account": "ACC"}).respond(
        json={"account": "ACC", "positions": [{"symbol": "PTT", "currentVolume": "300"}]}
    )
    adapter = make_adapter()
    positions = await adapter.get_positions("ACC")
    assert len(positions) == 1
    assert positions[0].symbol == "PTT" and positions[0].net_qty == 300
    assert positions[0].market is Market.SET
    await adapter.aclose()


@respx.mock
async def test_get_account_maps_the_cash_block() -> None:
    """SP supplies buying power + cash + credit, and NO margin block — it reports none."""
    respx.get(f"{_BASE}/account-info", params={"account": "ACC"}).respond(
        json={"lineAvailable": "12345.67", "cashBalance": "1.0", "creditLimit": 3920000.0}
    )
    adapter = make_adapter()
    info = await adapter.get_account("ACC")
    assert str(info.buying_power) == "12345.67"
    assert info.cash_balance == Decimal("1.0")
    assert info.credit_limit == Decimal("3920000.0")  # int/float both accepted
    assert info.account_type is AccountType.CASH
    # 🔴 absent, not zero — SP reports no margin at all
    assert info.equity is None and info.initial_margin is None
    await adapter.aclose()


@respx.mock
async def test_get_account_raises_rather_than_returning_zero_on_empty() -> None:
    """🔴 The replaced test asserted `buying_power == 0` here and was named ...defaults_to_zero.

    That was the last surviving instance of the silent degrade TK-0396 removed everywhere
    else: an empty body meant an UNREADABLE account, and a zero made it look like a flat one.
    SP's balance read is SET-only, so a TFEX account produces exactly this empty body.
    """
    respx.get(f"{_BASE}/account-info", params={"account": "ACC"}).respond(json={})
    adapter = make_adapter()
    with pytest.raises(StreamingProAccountUnavailable, match="no lineAvailable"):
        await adapter.get_account("ACC")
    await adapter.aclose()


async def test_amend_is_declared_cancel_replace() -> None:
    adapter = make_adapter()
    ack = await adapter.amend("cid", new_price=None, new_qty=None)
    assert not ack.ok
    assert ack.semantics == "cancel_replace"
    assert ack.reason is not None and "cancel+replace" in ack.reason
    await adapter.aclose()

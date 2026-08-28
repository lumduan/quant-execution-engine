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

    ⚠️ Updated 2026-08-27: the read is no longer SET-only, so "unreadable" now means
    **neither front answered**. Both are mocked empty here — that is the whole point. A
    single-front mock would leave the TFEX call unmocked and the test would fail for the
    wrong reason (an unmocked request), not because the adapter refused.
    """
    respx.get(f"{_BASE}/account-info", params={"account": "ACC"}).respond(json={})
    respx.get(f"{_BASE}/tfex/account-info", params={"account": "ACC"}).respond(json={})
    adapter = make_adapter()
    with pytest.raises(StreamingProAccountUnavailable, match="neither front"):
        await adapter.get_account("ACC")
    await adapter.aclose()


async def test_amend_is_declared_cancel_replace() -> None:
    adapter = make_adapter()
    ack = await adapter.amend("cid", new_price=None, new_qty=None)
    assert not ack.ok
    assert ack.semantics == "cancel_replace"
    assert ack.reason is not None and "cancel+replace" in ack.reason
    await adapter.aclose()


# ---------------------------------------------- SP TFEX balance (captured 2026-08-27)

# SHAPE from the live venue via the AWS bridge, 2026-08-27 22:40 BKK — the first real SP
# TFEX account body anyone here had. VALUES redacted 2026-08-28.
#
# ⚠️ It previously said "VERBATIM ... Not hand-written". After redaction that would have
# been FALSE, and a fixture that falsely claims to be verbatim is worse than one that
# admits it is synthetic — it invites the next reader to trust a number nobody measured.
#
# What IS still real and is why this fixture is worth having: the KEY SET, the TYPES, and
# which fields the TFEX front reports as a genuine 0 rather than omitting.
_TFEX_0500009 = {
    "creditLine": 50000.0,
    "excessEquity": 10000.44,
    "cashBalance": 10000.44,
    "equity": 10000.44,
    "totalMR": 0.0,
    "totalMM": 0.0,
    "totalFM": 0.0,
    "callForceFlag": "No",
    "callForceMargin": 0.0,
    "liquidationValue": 10000.44,
    "depositWithdrawal": 0.0,
    "callForceMarginMM": 0.0,
    "initialMargin": 0.0,
    "closingMethod": "Auto Net",
}
# Also verbatim: what BOTH fronts return for an account they do not hold. Note HTTP 200.
_NOT_FOUND_TFEX = {"code": "GWD-03", "message": "UserAccount not found of request account:X"}
_NOT_FOUND_SET = {"code": "FISGW-00", "message": "UserAccount not found of request account:X"}


@respx.mock
async def test_tfex_account_resolves_via_the_VENUE_not_the_account_number() -> None:
    """🔑 Market is decided by asking both fronts, never by pattern-matching the number.

    SET `0500007` and TFEX `0500009` differ by ONE DIGIT. Inferring market from that is
    exactly how a request gets silently answered by the wrong market — the hazard the
    bridge's own shape guard exists for. Here the SET front refuses and the TFEX front
    answers, and the adapter follows the venue.
    """
    respx.get(f"{_BASE}/account-info", params={"account": "0500009"}).respond(json=_NOT_FOUND_SET)
    respx.get(f"{_BASE}/tfex/account-info", params={"account": "0500009"}).respond(
        json=_TFEX_0500009
    )
    adapter = make_adapter()
    info = await adapter.get_account("0500009")

    assert info.account_type is AccountType.DERIVATIVE
    assert info.buying_power == Decimal("10000.44")  # excessEquity — the tradable figure
    assert info.equity == Decimal("10000.44")
    assert info.credit_limit == Decimal("50000.0")
    await adapter.aclose()


@respx.mock
async def test_a_REFUSAL_arrives_as_HTTP_200_and_must_not_read_as_a_zero_balance() -> None:
    """🔴 The venue answers 200 for an account it does not hold — refusal is in the BODY.

    An adapter keyed on `status_code == 200` would treat `GWD-03` as success and, if it
    then defaulted a missing balance, report a confident zero for an account it could not
    read. That is TK-0396 exactly. Both fronts refuse here; the adapter must RAISE.
    """
    respx.get(f"{_BASE}/account-info", params={"account": "9999999"}).respond(
        status_code=200, json=_NOT_FOUND_SET
    )
    respx.get(f"{_BASE}/tfex/account-info", params={"account": "9999999"}).respond(
        status_code=200, json=_NOT_FOUND_TFEX
    )
    adapter = make_adapter()
    with pytest.raises(StreamingProAccountUnavailable, match="neither front"):
        await adapter.get_account("9999999")
    await adapter.aclose()


@respx.mock
async def test_a_SET_account_never_reaches_the_TFEX_front() -> None:
    """Positive control: SET short-circuits, so the fallback is not always-on.

    Without this, the two tests above pass for a router that queries TFEX unconditionally
    — doubling venue reads on every SET balance for no reason.
    """
    set_route = respx.get(f"{_BASE}/account-info", params={"account": "0500007"}).respond(
        json={"lineAvailable": "38000.33", "cashBalance": "38000.33"}
    )
    tfex_route = respx.get(f"{_BASE}/tfex/account-info", params={"account": "0500007"}).respond(
        json=_NOT_FOUND_TFEX
    )
    adapter = make_adapter()
    info = await adapter.get_account("0500007")

    assert info.account_type is AccountType.CASH
    assert info.buying_power == Decimal("38000.33")
    assert set_route.called and not tfex_route.called, "SET must not fall through"
    await adapter.aclose()


@respx.mock
async def test_a_REPORTED_zero_margin_stays_zero_and_never_becomes_null() -> None:
    """The captured TFEX body reports totalMR/totalMM as 0.0 — genuinely zero, not absent.

    The mirror of the Liberator case: there the CASH entry OMITS the fields (-> null) while
    the DERIVATIVE entry REPORTS them as 0 (-> 0). Collapsing either direction is the bug.
    """
    respx.get(f"{_BASE}/account-info", params={"account": "0500009"}).respond(json=_NOT_FOUND_SET)
    respx.get(f"{_BASE}/tfex/account-info", params={"account": "0500009"}).respond(
        json=_TFEX_0500009
    )
    adapter = make_adapter()
    info = await adapter.get_account("0500009")

    assert info.initial_margin == Decimal("0.0")
    assert info.initial_margin is not None, "a REPORTED zero must not become null"
    assert info.maintenance_margin == Decimal("0.0")
    await adapter.aclose()

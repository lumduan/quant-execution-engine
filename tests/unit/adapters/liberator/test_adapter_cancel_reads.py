"""LiberatorAdapter cancel (cache + resolver), amend declaration, reads, heartbeat."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import pytest
import respx
from src.quant_execution_engine.adapters.base import AccountType
from src.quant_execution_engine.adapters.liberator.errors import (
    LiberatorAccountNotFound,
    LiberatorPositionsUncaptured,
    LiberatorTransportError,
)
from src.quant_execution_engine.contracts.enums import Market, OrderType, Side

from tests.conftest import make_order
from tests.unit.adapters.liberator.test_adapter_place import (
    _BASE,
    _liberator_order,
    _ok_place,
    make_adapter,
)


@respx.mock
async def test_cancel_uses_cached_order_no_from_place() -> None:
    respx.post(f"{_BASE}/order/place/set").respond(json=_ok_place("3064"))
    cancel_route = respx.post(f"{_BASE}/order/cancelled/set").respond(
        json={"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}
    )
    adapter = make_adapter()
    order = _liberator_order()
    await adapter.place(order)
    ack = await adapter.cancel(order.client_order_id)
    assert ack.ok
    sent = json.loads(cancel_route.calls.last.request.content)
    assert sent == {"orderNo": ["3064"], "pin": "987654"}
    await adapter.aclose()


@respx.mock
async def test_cancel_falls_back_to_injected_resolver_and_market_routes() -> None:
    cancel_route = respx.post(f"{_BASE}/order/cancelled/tfex").respond(
        json={"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}
    )

    async def resolver(client_order_id: str) -> tuple[str, Market] | None:
        return ("9001", Market.TFEX)

    adapter = make_adapter(resolve_order=resolver)
    ack = await adapter.cancel("0" * 8)
    assert ack.ok
    assert cancel_route.called
    await adapter.aclose()


async def test_cancel_without_any_mapping_is_not_ok() -> None:
    adapter = make_adapter()
    ack = await adapter.cancel("unknown-cid")
    assert not ack.ok
    assert ack.reason is not None and "no broker_order_id mapping" in ack.reason


@respx.mock
async def test_cancel_venue_reject_and_transport_failure_are_not_ok() -> None:
    respx.post(f"{_BASE}/order/cancelled/set").respond(
        json={"success": True, "data": {"errorCode": 12, "errMsg": "too late to cancel"}}
    )

    async def resolver(client_order_id: str) -> tuple[str, Market] | None:
        return ("3064", Market.SET)

    adapter = make_adapter(resolve_order=resolver)
    ack = await adapter.cancel("cid-1")
    assert not ack.ok and ack.reason is not None and "too late to cancel" in ack.reason

    respx.post(f"{_BASE}/order/cancelled/set").respond(status_code=502)
    ack = await adapter.cancel("cid-1")
    assert not ack.ok and ack.reason is not None and "502" in ack.reason
    await adapter.aclose()


async def test_amend_declares_cancel_replace_and_never_fakes_success() -> None:
    adapter = make_adapter()
    ack = await adapter.amend("cid-1", new_price=Decimal("10"), new_qty=5)
    assert not ack.ok
    assert ack.semantics == "cancel_replace"
    assert ack.reason is not None and "router cancel+replace" in ack.reason


def _orders_body(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "success": True,
        "data": {"errorCode": 0, "errMsg": "", "result": {"list": items}},
    }


@respx.mock
async def test_fetch_venue_orders_and_get_open_orders_view() -> None:
    items = [
        {  # open SET limit buy — representable
            "orderNo": "3064",
            "accountNo": "70000002",
            "symbol": "PTT",
            "side": "B",
            "priceType": "LIMIT",
            "volume": 100,
            "matched": 0,
            "balance": 100,
            "cancelled": 0,
            "price": "33.50",
            "status": "PENDING",
            "statusShow": "O",
            "rejectCode": "",
            "validityType": "Day",
        },
        {  # cancelled row — not open
            "orderNo": "3065",
            "symbol": "PTT",
            "side": "S",
            "priceType": "LIMIT",
            "volume": 100,
            "matched": 0,
            "balance": 0,
            "cancelled": 100,
            "price": "34.00",
            "status": "CANCELLED",
            "rejectCode": "",
        },
        {  # negative-price futures spread — unrepresentable, skipped
            "orderNo": "3066",
            "symbol": "S50U25Z25",
            "side": "S",
            "priceType": "LIMIT",
            "position": "Close",
            "volume": 2,
            "matched": 0,
            "balance": 2,
            "cancelled": 0,
            "price": "-0.50",
            "status": "PENDING",
            "rejectCode": "",
        },
    ]
    respx.get(f"{_BASE}/orders/70000002").respond(json=_orders_body(items))
    adapter = make_adapter()
    raw = await adapter.fetch_venue_orders("70000002")
    assert [item.order_no for item in raw] == ["3064", "3065", "3066"]
    open_orders = await adapter.get_open_orders("70000002")
    assert len(open_orders) == 1
    view = open_orders[0]
    assert view.symbol == "PTT"
    assert view.side is Side.BUY
    assert view.order_type is OrderType.LIMIT
    assert view.price == Decimal("33.50")
    # Deterministic placeholder id: same venue row -> same view id.
    again = (await adapter.get_open_orders("70000002"))[0]
    assert again.client_order_id == view.client_order_id
    await adapter.aclose()


# ── reads (TK-0396) ────────────────────────────────────────────────────────────
# 🔴 The bodies below are the REAL captured shape (2026-08-24, AWS node), not invented.
# The fixtures these replaced mocked `data.positions[]` + `data.summary.buying_power` --
# a body the venue cannot produce -- which is why a broken adapter tested green for weeks.
# Wire record: docs/reference/liberator-account-reads.md (umbrella).


def _profile(*accounts: dict[str, Any]) -> dict[str, Any]:
    """The bridge envelope for GET /va/profile. `data` is ALWAYS None on this route."""
    return {
        "success": True,
        "data": None,
        "message": "Profile data retrieved successfully",
        "error_code": None,
        "raw_response": {
            "errorCode": 0,
            "errMsg": "",
            "result": {"libfam": True, "accounts": list(accounts), "watchList": []},
        },
    }


def _cash_account(acct: str, line: Any) -> dict[str, Any]:
    return {
        "accountNo": acct,
        "type": "CASH BALANCE",
        "creditLimit": 500000,
        "lineAvailable": line,
        "cashBalance": line,
        "withdrawAvailable": line,
        "investorId": acct[:-1],
        "amount": 0,
        "marketVal": 0,
        "unrealizedPL": 0,
        "unrealizedPLPercent": 0,
        "realizedPL": 0,
        "stocks": [],
    }


def _deriv_account(acct: str, line: float) -> dict[str, Any]:
    """A DERIVATIVE entry — the captured 2026-08-24 shape, margin block included."""
    return {
        **_cash_account(acct, line),
        "type": "DERIVATIVE",
        "equity": line,
        "excessEquity": line,
        "totalMr": 0,
        "totalMm": 0,
        "callForceFlag": "No",
        "callForceMr": 0,
    }


@respx.mock
async def test_get_account_maps_the_derivative_margin_block() -> None:
    """TFEX accounts carry equity + margin; the SET sibling in the same response must not."""
    respx.get(f"{_BASE}/profile").respond(
        json=_profile(_cash_account("70000002", 50000.11), _deriv_account("70000007", 13000.22))
    )
    adapter = make_adapter()

    deriv = await adapter.get_account("70000007")
    assert deriv.account_type is AccountType.DERIVATIVE
    assert deriv.equity == Decimal("13000.22")
    assert deriv.excess_equity == Decimal("13000.22")
    assert deriv.initial_margin == Decimal("0")  # totalMr -> IM; a REAL zero, not an absence
    assert deriv.maintenance_margin == Decimal("0")
    assert deriv.cash_balance == Decimal("13000.22")

    # 🔑 the contrast is the test: same response, same call, and the cash account carries NO
    # margin block. If the mapper leaked it across, the model itself would refuse.
    cash = await adapter.get_account("70000002")
    assert cash.account_type is AccountType.CASH
    assert cash.equity is None and cash.initial_margin is None
    assert cash.withdrawable == Decimal("50000.11")
    await adapter.aclose()


@respx.mock
async def test_get_account_reads_line_available_from_profile() -> None:
    """The fix: balance comes from /va/profile, matched on accountNo."""
    respx.get(f"{_BASE}/profile").respond(
        json=_profile(_cash_account("70000002", 50000.11), _cash_account("70000007", 13000.22))
    )
    adapter = make_adapter()
    account = await adapter.get_account("70000007")
    assert account.buying_power == Decimal("13000.22")  # the SECOND entry, not the first
    assert account.account == "70000007"
    await adapter.aclose()


@respx.mock
async def test_get_account_accepts_int_and_float_money() -> None:
    """⚠️ The venue sends float when non-zero and **int** at zero, in the same field."""
    respx.get(f"{_BASE}/profile").respond(json=_profile(_cash_account("70000012", 0)))
    adapter = make_adapter()
    account = await adapter.get_account("70000012")
    assert account.buying_power == Decimal("0")
    await adapter.aclose()


@respx.mock
async def test_get_account_raises_rather_than_returning_zero_for_unknown_account() -> None:
    """🔴 The mutation that matters: a 0 here would be indistinguishable from a real zero.

    The replaced test asserted `buying_power == 0` for a missing shape, encoding the
    silent degrade as *intended* -- and it was green against the live bridge, because
    that branch was the only one production ever took.
    """
    respx.get(f"{_BASE}/profile").respond(json=_profile(_cash_account("70000002", 50000.11)))
    adapter = make_adapter()
    with pytest.raises(LiberatorAccountNotFound):
        await adapter.get_account("99999999")
    await adapter.aclose()


@respx.mock
async def test_get_account_refuses_on_venue_errmsg_even_though_success_is_true() -> None:
    """🔴 `success: true` on a REFUSED account is the bridge's live behaviour (GH #208).

    A caller trusting `success` gets an authoritative wrong answer, so the adapter reads
    `raw_response.errMsg` instead. Positive control: the happy-path tests above share this
    envelope with an empty errMsg and pass.
    """
    respx.get(f"{_BASE}/profile").respond(
        json={
            "success": True,
            "data": None,
            "message": "Profile data retrieved successfully",
            "error_code": None,
            "raw_response": {
                "errorCode": 0,
                "errMsg": "Error AccessAuthen: Account Not Authorized",
                "result": {"accounts": []},
            },
        }
    )
    adapter = make_adapter()
    with pytest.raises(LiberatorAccountNotFound, match="Account Not Authorized"):
        await adapter.get_account("70000002")
    await adapter.aclose()


# --------------------------------------------------- positions (implemented 2026-08-28)
#
# 🔑 Schema below is VERBATIM-SHAPED from the first populated capture (umbrella
# `docs/reference/liberator-account-reads.md` §2.2) — the field names, their types and
# the `sideShow` literals are exactly what the venue sent. The ACCOUNT NUMBERS,
# SYMBOLS and QUANTITIES are synthetic: real ones do not belong in a PUBLIC repo.
# The synthetic accounts keep the venue's grammar (8 digits, suffix 2 = CASH/SET,
# 7 = DERIVATIVE/TFEX) because the adapter reads market from that suffix.

_ACCT_TFEX = "70000007"
_ACCT_SET = "70000002"

# 17 fields — the TFEX shape. optVol / positionShow / startVol are TFEX-ONLY.
_TFEX_ROW = {
    "symbol": "AAAZ26",
    "symbolDisplay": "AAAZ26",
    "sideShow": "Long",
    "actualVol": 3,
    "avaiVol": 3,
    "startVol": 3,
    "avg": 100.5,
    "amount": 301.5,
    "optVol": 0,
    "positionShow": "Open",
    "realizedPL": 0,
    "marketPrice": 101.0,
    "marketVal": 303,
    "nameEN": "AAA Futures",
    "nameTH": "AAA",
    "unrealizedPL": 1.5,
    "unrealizedPLPercent": 0.5,
}
# 14 fields — SET is a strict SUBSET, and sideShow is the EMPTY STRING.
_SET_ROW = {
    "symbol": "BBB",
    "symbolDisplay": "BBB",
    "sideShow": "",
    "actualVol": 100,
    "avaiVol": 100,
    "avg": 10.0,
    "amount": 1000,
    "realizedPL": 0,
    "marketPrice": 10.5,
    "marketVal": 1050,
    "nameEN": "BBB Public Co",
    "nameTH": "BBB",
    "unrealizedPL": 50,
    "unrealizedPLPercent": 5.0,
}


def _portfolio(*rows: dict[str, object], err: str = "") -> dict[str, object]:
    """The bridge envelope, exactly as captured — note `success` is true even on refusal."""
    return {
        "success": True,
        "data": None,
        "error_code": None,
        "raw_response": {
            "errorCode": 0,
            "errMsg": err,
            "result": {"list": [r["symbol"] for r in rows], "stock": list(rows)},
        },
    }


@respx.mock
async def test_get_positions_parses_the_TFEX_shape_with_a_real_side() -> None:
    """17 fields, sideShow 'Long' -> Side.BUY, market from the suffix-7 grammar."""
    respx.get(f"{_BASE}/portfolio/get/{_ACCT_TFEX}").respond(json=_portfolio(_TFEX_ROW))
    adapter = make_adapter()
    positions = await adapter.get_positions(_ACCT_TFEX)
    assert len(positions) == 1
    p = positions[0]
    assert p.symbol == "AAAZ26" and p.net_qty == 3
    assert p.market is Market.TFEX
    assert p.side is Side.BUY
    await adapter.aclose()


@respx.mock
async def test_a_SET_row_yields_side_None_because_the_venue_SENDS_AN_EMPTY_STRING() -> None:
    """🔑 SET sends `sideShow: ""` — not a missing key, not "Long".

    `None` here means *the venue did not distinguish*, never *flat* and never *long*.
    A SET equity cannot be short, so the venue declines to say; inventing BUY would be
    asserting something the venue withheld.
    """
    respx.get(f"{_BASE}/portfolio/get/{_ACCT_SET}").respond(json=_portfolio(_SET_ROW))
    adapter = make_adapter()
    positions = await adapter.get_positions(_ACCT_SET)
    assert positions[0].side is None
    assert positions[0].market is Market.SET
    await adapter.aclose()


@respx.mock
async def test_sideShow_Short_maps_to_SELL() -> None:
    """The other observed literal. Both directions matter: a mis-sided leg is a money bug."""
    row = {**_TFEX_ROW, "sideShow": "Short", "symbol": "CCCZ26", "symbolDisplay": "CCCZ26"}
    respx.get(f"{_BASE}/portfolio/get/{_ACCT_TFEX}").respond(json=_portfolio(row))
    adapter = make_adapter()
    assert (await adapter.get_positions(_ACCT_TFEX))[0].side is Side.SELL
    await adapter.aclose()


@respx.mock
async def test_a_REFUSED_account_RAISES_and_never_renders_as_an_empty_portfolio() -> None:
    """🔴 TK-0420, confirmed against a populated response, not inferred from two empty ones.

    The bridge returns `success: true` AND `errorCode: 0` for an account the venue
    refused — byte-identical to an authorized-but-flat one. Only `errMsg` differs. A
    parser keyed on either of the first two reports a DENIED account as a FLAT one.
    """
    respx.get(f"{_BASE}/portfolio/get/{_ACCT_TFEX}").respond(
        json=_portfolio(err="Error AccessAuthen: Account Not Authorized")
    )
    adapter = make_adapter()
    with pytest.raises(LiberatorAccountNotFound):
        await adapter.get_positions(_ACCT_TFEX)
    await adapter.aclose()


@respx.mock
async def test_an_authorized_but_FLAT_account_returns_an_empty_list() -> None:
    """The positive control for the test above: empty must still be reachable.

    Without this, `raise on empty` would pass the refusal test and be wrong.
    """
    respx.get(f"{_BASE}/portfolio/get/{_ACCT_SET}").respond(json=_portfolio())
    adapter = make_adapter()
    assert await adapter.get_positions(_ACCT_SET) == []
    await adapter.aclose()


async def test_a_bare_investorId_is_REFUSED_rather_than_read_as_flat() -> None:
    """🔴 The false-absence trap the account-grammar guard exists for.

    The venue ACCEPTS a 7-digit investorId, answers `errMsg: ""` and returns an empty
    result — indistinguishable from a genuinely flat account. Refusing here means
    "I cannot read this" can never render as "there is nothing here".
    """
    adapter = make_adapter()
    with pytest.raises(LiberatorAccountNotFound, match="8-digit"):
        await adapter.get_positions("7000000")
    await adapter.aclose()


@respx.mock
async def test_an_UNMAPPED_sideShow_literal_raises_instead_of_guessing() -> None:
    """If the venue's vocabulary grows, fail loudly — a guessed side is a money bug."""
    row = {**_TFEX_ROW, "sideShow": "Hedged"}
    respx.get(f"{_BASE}/portfolio/get/{_ACCT_TFEX}").respond(json=_portfolio(row))
    adapter = make_adapter()
    with pytest.raises(LiberatorTransportError, match="unmapped sideShow"):
        await adapter.get_positions(_ACCT_TFEX)
    await adapter.aclose()


@respx.mock
async def test_UNKNOWN_venue_fields_are_ignored_not_rejected() -> None:
    """Forward-compatibility, and it is evidence-based rather than a habit.

    The capture proved the element is market-dependent (17 TFEX / 14 SET) and CANNOT
    prove what a short-SET or expiring-TFEX row carries. Rejecting an unrecognised key
    would turn a venue addition into an outage.
    """
    row = {**_TFEX_ROW, "someFieldInventedNextYear": {"nested": True}}
    respx.get(f"{_BASE}/portfolio/get/{_ACCT_TFEX}").respond(json=_portfolio(row))
    adapter = make_adapter()
    assert (await adapter.get_positions(_ACCT_TFEX))[0].symbol == "AAAZ26"
    await adapter.aclose()


@respx.mock
async def test_heartbeat_healthy_requires_status_and_auth_token() -> None:
    route = respx.get(f"{_BASE}/order/health/set")
    adapter = make_adapter()

    route.respond(json={"status": "healthy", "auth_token_available": True})
    assert await adapter.heartbeat() is True
    assert adapter.last_heartbeat_ok is True

    route.respond(json={"status": "healthy", "auth_token_available": False})
    assert await adapter.heartbeat() is False  # dead broker session

    route.respond(json={"status": "unhealthy", "auth_token_available": True})
    assert await adapter.heartbeat() is False

    route.respond(status_code=503)
    assert await adapter.heartbeat() is False  # transport failure never raises
    assert adapter.last_heartbeat_ok is False
    await adapter.aclose()


@respx.mock
async def test_pin_never_logged_on_place_cancel_amend(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Required case 9: no log record in any order path carries the PIN."""
    pin = "987654"
    respx.post(f"{_BASE}/order/place/set").respond(json=_ok_place())
    respx.post(f"{_BASE}/order/cancelled/set").respond(
        json={"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}
    )
    adapter = make_adapter()
    order = make_order(broker="liberator", price="123.45")
    with caplog.at_level(logging.DEBUG):
        await adapter.place(order)
        await adapter.cancel(order.client_order_id)
        await adapter.amend(order.client_order_id, new_price=Decimal("10"))
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert pin not in rendered
    assert order.account not in rendered
    await adapter.aclose()


def test_new_read_codes_are_mapped_not_silently_400() -> None:
    """TK-0396: both new codes must be PRESENT in the status map, with the right status.

    🔑 The positive control matters here. Asserting "not 500" would be met by an unmapped
    code too, because `_status_for` falls back to **400** — so a forgotten mapping would
    pass a not-500 check silently. Pin presence AND the exact status.
    """
    from src.quant_execution_engine.api.error_handlers import _STATUS_BY_CODE, _status_for

    for exc, expected in (
        (LiberatorAccountNotFound("x"), 404),
        (LiberatorPositionsUncaptured("x"), 501),
    ):
        assert exc.code in _STATUS_BY_CODE, f"{exc.code} missing -> silent 400 fallback"
        assert _status_for(exc) == expected

    # and they must stay distinct: "this account does not exist" and "we cannot read positions
    # at all" are different failures, and collapsing them would hide one behind the other.
    assert LiberatorAccountNotFound.code != LiberatorPositionsUncaptured.code

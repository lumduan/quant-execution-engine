"""LiberatorAdapter cancel (cache + resolver), amend declaration, reads, heartbeat."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import pytest
import respx
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
            "accountNo": "70173292",
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
    respx.get(f"{_BASE}/orders/70173292").respond(json=_orders_body(items))
    adapter = make_adapter()
    raw = await adapter.fetch_venue_orders("70173292")
    assert [item.order_no for item in raw] == ["3064", "3065", "3066"]
    open_orders = await adapter.get_open_orders("70173292")
    assert len(open_orders) == 1
    view = open_orders[0]
    assert view.symbol == "PTT"
    assert view.side is Side.BUY
    assert view.order_type is OrderType.LIMIT
    assert view.price == Decimal("33.50")
    # Deterministic placeholder id: same venue row -> same view id.
    again = (await adapter.get_open_orders("70173292"))[0]
    assert again.client_order_id == view.client_order_id
    await adapter.aclose()


@respx.mock
async def test_get_positions_and_account_parse_portfolio() -> None:
    respx.get(f"{_BASE}/portfolio/get/70173292").respond(
        json={
            "success": True,
            "message": "ok",
            "data": {
                "account_number": "70173292",
                "positions": [
                    {"symbol": "PTT", "quantity": 300},
                    {"symbol": "CPALL", "quantity": -100},
                    "garbage",
                    {"symbol": 42, "quantity": "bad"},
                ],
                "summary": {"buying_power": 125000.50},
            },
        }
    )
    adapter = make_adapter()
    positions = await adapter.get_positions("70173292")
    assert [(p.symbol, p.net_qty) for p in positions] == [("PTT", 300), ("CPALL", -100)]
    account = await adapter.get_account("70173292")
    assert account.buying_power == Decimal("125000.5")
    await adapter.aclose()


@respx.mock
async def test_get_account_defaults_to_zero_when_shape_missing() -> None:
    respx.get(f"{_BASE}/portfolio/get/70173292").respond(json={"success": False})
    adapter = make_adapter()
    account = await adapter.get_account("70173292")
    assert account.buying_power == Decimal("0")
    positions = await adapter.get_positions("70173292")
    assert positions == []
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

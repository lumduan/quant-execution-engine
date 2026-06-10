"""Repositories: SQL text/params, asyncpg error mapping, no order_events writes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.contracts.errors import IllegalTransition
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.errors import DuplicateOrderSignal

from tests._fakes import FakeConn, FakePool, check_violation, unique_violation
from tests.conftest import make_order

_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def order_record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "client_order_id": "11111111-2222-4333-8444-555555555555",
        "broker": "sim",
        "broker_order_id": None,
        "account": "ACC-TEST",
        "symbol": "PTT",
        "market": "SET",
        "side": "BUY",
        "order_type": "LIMIT",
        "price": Decimal("123.456789"),
        "stop_price": None,
        "quantity": 100,
        "display_qty": None,
        "tif": "DAY",
        "position_effect": None,
        "status": "PENDING_NEW",
        "reject_reason": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return base


async def test_insert_order_sql_params_and_duplicate_mapping() -> None:
    conn = FakeConn(execute_results=["INSERT 0 1"])
    order = make_order()
    await repositories.insert_order(FakePool(conn), order)
    method, sql, args = conn.calls[0]
    assert "INSERT INTO execution.orders" in sql
    assert "order_events" not in sql
    assert args[0] == order.client_order_id
    assert args[9] == order.quantity
    dup = FakeConn(raise_map={"INSERT INTO execution.orders": unique_violation()})
    with pytest.raises(DuplicateOrderSignal):
        await repositories.insert_order(FakePool(dup), order)


async def test_fetch_order_and_result_aggregate() -> None:
    conn = FakeConn(fetchrow_results=[None])
    assert await repositories.fetch_order(FakePool(conn), "x") is None
    conn = FakeConn(
        fetchrow_results=[
            order_record(
                status="PARTIALLY_FILLED",
                broker_order_id="SIM-1",
                filled_qty=40,
                fill_notional=Decimal("4000"),
            )
        ]
    )
    row = await repositories.fetch_order_result(FakePool(conn), "x")
    assert row is not None
    assert row.filled_qty == 40
    assert row.avg_fill_price == Decimal("100")
    empty = FakeConn(fetchrow_results=[order_record(filled_qty=0, fill_notional=None)])
    no_fills = await repositories.fetch_order_result(FakePool(empty), "x")
    assert no_fills is not None and no_fills.avg_fill_price is None


async def test_ack_order_single_statement_and_rowcount_guard() -> None:
    conn = FakeConn(execute_results=["UPDATE 1"])
    await repositories.ack_order(FakePool(conn), "cid", "SIM-1")
    _, sql, args = conn.calls[0]
    assert "status = 'NEW'" in sql and "broker_order_id = $2" in sql
    assert "status = 'PENDING_NEW'" in sql  # guarded WHERE
    assert args == ("cid", "SIM-1")
    stale = FakeConn(execute_results=["UPDATE 0"])
    with pytest.raises(IllegalTransition):
        await repositories.ack_order(FakePool(stale), "cid", "SIM-1")


async def test_update_status_app_guard_db_backstop_and_noop() -> None:
    # Legal transition executes the UPDATE.
    conn = FakeConn(fetchrow_results=[order_record(status="NEW")], execute_results=["UPDATE 1"])
    await repositories.update_status(FakePool(conn), "cid", OrderState.PENDING_CANCEL)
    assert any("SET status = $2" in sql for _, sql, _ in conn.calls)
    # Same-status: no UPDATE issued.
    noop = FakeConn(fetchrow_results=[order_record(status="NEW")])
    await repositories.update_status(FakePool(noop), "cid", OrderState.NEW)
    assert all(method != "execute" for method, _, _ in noop.calls)
    # App-side guard rejects without touching the DB.
    guarded = FakeConn(fetchrow_results=[order_record(status="FILLED")])
    with pytest.raises(IllegalTransition):
        await repositories.update_status(FakePool(guarded), "cid", OrderState.NEW)
    assert all(method != "execute" for method, _, _ in guarded.calls)
    # Unknown order.
    missing = FakeConn(fetchrow_results=[None])
    with pytest.raises(IllegalTransition):
        await repositories.update_status(FakePool(missing), "cid", OrderState.NEW)
    # DB backstop: 23514 maps to IllegalTransition.
    backstop = FakeConn(
        fetchrow_results=[order_record(status="NEW")],
        raise_map={"SET status = $2": check_violation()},
    )
    with pytest.raises(IllegalTransition):
        await repositories.update_status(FakePool(backstop), "cid", OrderState.FILLED)


async def test_apply_fill_flips_states_atomically() -> None:
    # Partial: 40 of 100 -> PARTIALLY_FILLED.
    conn = FakeConn(fetchval_results=[40, "NEW"], execute_results=["INSERT 0 1", "UPDATE 1"])
    state = await repositories.apply_fill(
        FakePool(conn),
        "cid",
        broker_fill_id="F-1",
        price=Decimal("100"),
        quantity=40,
        exec_ts=_NOW,
        total_quantity=100,
    )
    assert state is OrderState.PARTIALLY_FILLED
    insert_sql = conn.calls[0][1]
    assert "ON CONFLICT (client_order_id, broker_fill_id) DO NOTHING" in insert_sql
    # Completing: sum reaches 100 -> FILLED.
    conn = FakeConn(
        fetchval_results=[100, "PARTIALLY_FILLED"],
        execute_results=["INSERT 0 1", "UPDATE 1"],
    )
    state = await repositories.apply_fill(
        FakePool(conn),
        "cid",
        broker_fill_id="F-2",
        price=Decimal("100"),
        quantity=60,
        exec_ts=_NOW,
        total_quantity=100,
    )
    assert state is OrderState.FILLED
    # Redelivery: aggregate unchanged and status already at target -> no UPDATE.
    conn = FakeConn(fetchval_results=[40, "PARTIALLY_FILLED"], execute_results=["INSERT 0 0"])
    state = await repositories.apply_fill(
        FakePool(conn),
        "cid",
        broker_fill_id="F-1",
        price=Decimal("100"),
        quantity=40,
        exec_ts=_NOW,
        total_quantity=100,
    )
    assert state is OrderState.PARTIALLY_FILLED
    updates = [c for c in conn.calls if c[0] == "execute" and "SET status" in c[1]]
    assert updates == []
    # 23514 inside the transaction maps to IllegalTransition.
    bad = FakeConn(
        fetchval_results=[100, "CANCELLED"],
        execute_results=["INSERT 0 1"],
        raise_map={"SET status = $2": check_violation()},
    )
    with pytest.raises(IllegalTransition):
        await repositories.apply_fill(
            FakePool(bad),
            "cid",
            broker_fill_id="F-3",
            price=Decimal("100"),
            quantity=100,
            exec_ts=_NOW,
            total_quantity=100,
        )


async def test_set_reject_reason_and_open_orders() -> None:
    conn = FakeConn(execute_results=["UPDATE 1"])
    await repositories.set_reject_reason(FakePool(conn), "cid", "why")
    assert "reject_reason = $2" in conn.calls[0][1]
    rows = FakeConn(
        fetch_results=[[order_record(status="NEW"), order_record(status="PARTIALLY_FILLED")]]
    )
    open_orders = await repositories.fetch_open_orders(FakePool(rows))
    assert [r.status for r in open_orders] == [
        OrderState.NEW,
        OrderState.PARTIALLY_FILLED,
    ]
    sql = rows.calls[0][1]
    assert "('NEW', 'PARTIALLY_FILLED')" in sql

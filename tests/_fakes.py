"""Test doubles: FakeConn/FakePool (asyncpg), FakeRedis, and MemStore.

``FakeConn``/``FakePool`` serve the SQL-level repository tests (scripted
results + recorded calls + real asyncpg exception injection). ``MemStore``
is a behavioural stand-in for :mod:`db.repositories` used by router/API
tests — same signatures, in-memory state, same typed errors.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.contracts.capabilities import CapabilitySet, lookup
from src.quant_execution_engine.contracts.enums import Broker, Market, OrderState
from src.quant_execution_engine.contracts.errors import IllegalTransition
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core import state_machine
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.errors import DuplicateOrderSignal
from src.quant_execution_engine.db.models import OrderResultRow, OrderRow
from src.quant_execution_engine.events.hub import get_event_hub
from src.quant_execution_engine.events.models import FillEvent

# --------------------------------------------------------------------- asyncpg


class FakeConn:
    """Scripted asyncpg connection: queues of results + substring-keyed raises."""

    def __init__(
        self,
        *,
        execute_results: list[str] | None = None,
        fetchrow_results: list[dict[str, Any] | None] | None = None,
        fetchval_results: list[Any] | None = None,
        fetch_results: list[list[dict[str, Any]]] | None = None,
        raise_map: dict[str, Exception] | None = None,
    ) -> None:
        self.execute_results = execute_results or []
        self.fetchrow_results = fetchrow_results or []
        self.fetchval_results = fetchval_results or []
        self.fetch_results = fetch_results or []
        self.raise_map = raise_map or {}
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []

    def _maybe_raise(self, sql: str) -> None:
        for token, exc in self.raise_map.items():
            if token in sql:
                raise exc

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args))
        self._maybe_raise(sql)
        return self.execute_results.pop(0) if self.execute_results else "UPDATE 1"

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(("fetchrow", sql, args))
        self._maybe_raise(sql)
        return self.fetchrow_results.pop(0) if self.fetchrow_results else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchval", sql, args))
        self._maybe_raise(sql)
        return self.fetchval_results.pop(0) if self.fetchval_results else None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args))
        self._maybe_raise(sql)
        return self.fetch_results.pop(0) if self.fetch_results else []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        yield


class FakePool:
    """Delegates to a FakeConn; ``acquire()`` yields the same conn."""

    def __init__(self, conn: FakeConn | None = None) -> None:
        self.conn = conn or FakeConn()

    async def execute(self, sql: str, *args: Any) -> str:
        return await self.conn.execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return await self.conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self.conn.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return await self.conn.fetch(sql, *args)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConn]:
        yield self.conn

    async def close(self) -> None:
        return None


def unique_violation() -> asyncpg.exceptions.UniqueViolationError:
    return asyncpg.exceptions.UniqueViolationError("duplicate key value")


def check_violation() -> asyncpg.exceptions.CheckViolationError:
    return asyncpg.exceptions.CheckViolationError("illegal transition")


# ----------------------------------------------------------------------- redis


class FakeRedis:
    """Minimal redis.asyncio stand-in with TTL bookkeeping + failure switch."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    def _maybe_fail(self) -> None:
        if self.fail:
            raise ConnectionError("fake redis down")

    async def get(self, key: str) -> str | None:
        self._maybe_fail()
        return self.store.get(key)

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        self._maybe_fail()
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def delete(self, key: str) -> int:
        self._maybe_fail()
        return 1 if self.store.pop(key, None) is not None else 0

    async def incr(self, key: str) -> int:
        self._maybe_fail()
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def expire(self, key: str, ttl: int) -> bool:
        self._maybe_fail()
        self.ttls[key] = ttl
        return True

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        self._maybe_fail()
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------- broker adapter


class StubBrokerAdapter(BrokerAdapter):
    """Configurable in-memory BrokerAdapter for router/stage tests.

    Records amend/cancel/place calls; ``amend`` returns a scripted
    :class:`AmendAck` (default ok) or raises a scripted exception.
    """

    def __init__(
        self,
        *,
        broker: Broker = Broker.SETTRADE,
        amend_ack: AmendAck | None = None,
        amend_raises: Exception | None = None,
    ) -> None:
        super().__init__()
        self.broker = broker  # type: ignore[misc]  # per-instance override for tests
        self._amend_ack = amend_ack or AmendAck(ok=True, semantics="native")
        self._amend_raises = amend_raises
        self.amend_calls: list[tuple[str, Decimal | None, int | None]] = []
        self.cancel_calls: list[str] = []
        self.place_calls: list[NormalizedOrder] = []

    async def place(self, order: NormalizedOrder) -> PlaceAck:
        self.place_calls.append(order)
        return PlaceAck(broker_order_id=f"STUB-{order.client_order_id[:8]}")

    async def cancel(self, client_order_id: str) -> CancelAck:
        self.cancel_calls.append(client_order_id)
        return CancelAck(ok=True)

    async def amend(
        self,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        self.amend_calls.append((client_order_id, new_price, new_qty))
        if self._amend_raises is not None:
            raise self._amend_raises
        return self._amend_ack

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        return []

    async def get_positions(self, account: str) -> list[Position]:
        return []

    async def get_account(self, account: str) -> AccountInfo:
        return AccountInfo(account=account, buying_power=Decimal("1000000000"))

    def capabilities(self) -> tuple[CapabilitySet, ...]:
        return (lookup(self.broker, Market.SET),)

    async def heartbeat(self) -> bool:
        return True


# -------------------------------------------------------------------- MemStore


class MemStore:
    """Behavioural in-memory stand-in for ``db.repositories``."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.fills: dict[str, list[dict[str, Any]]] = {}

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def seed(
        self, order: NormalizedOrder, status: OrderState, *, strategy_id: str | None = None
    ) -> None:
        """Insert directly at a given state (test fixture helper)."""
        now = self._now()
        self.orders[order.client_order_id] = {
            "client_order_id": order.client_order_id,
            "broker": order.broker,
            "broker_order_id": None if status is OrderState.PENDING_NEW else "B-SEED",
            "account": order.account,
            "symbol": order.symbol,
            "market": order.market,
            "side": order.side,
            "order_type": order.order_type,
            "price": order.price,
            "stop_price": order.stop_price,
            "quantity": order.quantity,
            "display_qty": order.display_qty,
            "tif": order.tif,
            "position_effect": order.position_effect,
            "status": status,
            "reject_reason": None,
            "strategy_id": strategy_id,
            "created_at": now,
            "updated_at": now,
        }
        self.fills.setdefault(order.client_order_id, [])

    async def insert_order(
        self, pool: Any, order: NormalizedOrder, strategy_id: str | None = None
    ) -> None:
        if order.client_order_id in self.orders:
            raise DuplicateOrderSignal(order.client_order_id)
        self.seed(order, OrderState.PENDING_NEW, strategy_id=strategy_id)
        hub = get_event_hub()
        if hub is not None:
            if strategy_id is not None:
                hub.register_strategy(order.client_order_id, strategy_id)
            hub.publish(
                client_order_id=order.client_order_id,
                engine_state=OrderState.PENDING_NEW,
                strategy_id=strategy_id,
            )

    async def fetch_order(self, pool: Any, client_order_id: str) -> OrderRow | None:
        row = self.orders.get(client_order_id)
        return OrderRow(**row) if row is not None else None

    async def fetch_order_result(self, pool: Any, client_order_id: str) -> OrderResultRow | None:
        row = self.orders.get(client_order_id)
        if row is None:
            return None
        fills = self.fills.get(client_order_id, [])
        filled = sum(f["quantity"] for f in fills)
        notional = sum((f["price"] * f["quantity"] for f in fills), Decimal(0)) if fills else None
        return OrderResultRow(**row, filled_qty=filled, fill_notional=notional)

    async def ack_order(self, pool: Any, client_order_id: str, broker_order_id: str) -> None:
        row = self.orders.get(client_order_id)
        if row is None or row["status"] is not OrderState.PENDING_NEW:
            raise IllegalTransition(
                "ack requires the order to be in PENDING_NEW",
                client_order_id=client_order_id,
            )
        row["status"] = OrderState.NEW
        row["broker_order_id"] = broker_order_id
        row["updated_at"] = self._now()
        hub = get_event_hub()
        if hub is not None:
            hub.publish(
                client_order_id=client_order_id,
                engine_state=OrderState.NEW,
                broker_order_id=broker_order_id,
            )

    async def replace_order(
        self,
        pool: Any,
        client_order_id: str,
        new_price: Decimal | None,
        new_qty: int | None,
    ) -> None:
        row = self.orders.get(client_order_id)
        if row is None or row["status"] is not OrderState.PENDING_REPLACE:
            raise IllegalTransition(
                "replace requires the order to be in PENDING_REPLACE",
                client_order_id=client_order_id,
            )
        row["status"] = OrderState.NEW
        if new_price is not None:
            row["price"] = new_price
        if new_qty is not None:
            row["quantity"] = new_qty
        row["updated_at"] = self._now()
        hub = get_event_hub()
        if hub is not None:
            hub.publish(
                client_order_id=client_order_id,
                engine_state=OrderState.NEW,
                price=new_price,
                quantity=new_qty,
            )

    async def update_status(self, pool: Any, client_order_id: str, new_status: OrderState) -> None:
        row = self.orders.get(client_order_id)
        if row is None:
            raise IllegalTransition(
                "cannot transition an unknown order", client_order_id=client_order_id
            )
        if row["status"] is new_status:
            return
        state_machine.assert_legal(row["status"], new_status, client_order_id=client_order_id)
        row["status"] = new_status
        row["updated_at"] = self._now()
        hub = get_event_hub()
        if hub is not None:
            hub.publish(
                client_order_id=client_order_id,
                engine_state=new_status,
                strategy_id=row["strategy_id"],
                broker_order_id=row["broker_order_id"],
            )

    async def set_reject_reason(self, pool: Any, client_order_id: str, reason: str) -> None:
        self.orders[client_order_id]["reject_reason"] = reason

    async def apply_fill(
        self,
        pool: Any,
        client_order_id: str,
        *,
        broker_fill_id: str,
        price: Decimal,
        quantity: int,
        exec_ts: datetime,
        total_quantity: int,
    ) -> OrderState:
        fills = self.fills.setdefault(client_order_id, [])
        newly_recorded = not any(f["broker_fill_id"] == broker_fill_id for f in fills)
        if newly_recorded:
            fills.append(
                {
                    "broker_fill_id": broker_fill_id,
                    "price": price,
                    "quantity": quantity,
                    "exec_ts": exec_ts,
                }
            )
        filled = sum(f["quantity"] for f in fills)
        target = OrderState.FILLED if filled >= total_quantity else OrderState.PARTIALLY_FILLED
        # Flip the status WITHOUT the publishing update_status — apply_fill emits a
        # single fill-bearing event (mirrors db.repositories.apply_fill).
        row = self.orders[client_order_id]
        if row["status"] is not target:
            state_machine.assert_legal(row["status"], target, client_order_id=client_order_id)
            row["status"] = target
            row["updated_at"] = self._now()
        hub = get_event_hub()
        if hub is not None and newly_recorded:
            hub.publish(
                client_order_id=client_order_id,
                engine_state=target,
                fill=FillEvent(
                    broker_fill_id=broker_fill_id,
                    price=price,
                    quantity=quantity,
                    exec_ts=exec_ts,
                ),
            )
        return target

    async def fetch_open_orders(self, pool: Any) -> list[OrderRow]:
        return [
            OrderRow(**row)
            for row in self.orders.values()
            if row["status"] in (OrderState.NEW, OrderState.PARTIALLY_FILLED)
        ]

    async def fetch_orders_for_reconcile(
        self,
        pool: Any,
        broker: Any,
        *,
        include_pending_replace: bool = False,
    ) -> list[OrderRow]:
        wanted = [
            OrderState.PENDING_NEW,
            OrderState.NEW,
            OrderState.PARTIALLY_FILLED,
            OrderState.PENDING_CANCEL,
        ]
        if include_pending_replace:
            wanted.append(OrderState.PENDING_REPLACE)
        return [
            OrderRow(**row)
            for row in self.orders.values()
            if row["broker"] == broker and row["status"] in wanted
        ]

    async def fetch_client_order_ids_for_strategy(self, pool: Any, strategy_id: str) -> set[str]:
        return {cid for cid, row in self.orders.items() if row["strategy_id"] == strategy_id}


_REPO_FUNCTIONS = (
    "insert_order",
    "fetch_order",
    "fetch_order_result",
    "ack_order",
    "replace_order",
    "update_status",
    "set_reject_reason",
    "apply_fill",
    "fetch_open_orders",
    "fetch_orders_for_reconcile",
    "fetch_client_order_ids_for_strategy",
)


def patch_repositories(monkeypatch: pytest.MonkeyPatch, store: MemStore) -> None:
    """Point every ``db.repositories`` function at the MemStore."""
    for name in _REPO_FUNCTIONS:
        monkeypatch.setattr(repositories, name, getattr(store, name))

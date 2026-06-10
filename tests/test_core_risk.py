"""PTRM gate: cap boundaries, throttles, stage-aware fail policy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from src.quant_execution_engine.contracts.errors import RiskRejected
from src.quant_execution_engine.core.risk import RiskGate

from tests._fakes import FakeRedis
from tests.conftest import make_order, make_settings


async def test_quantity_cap_boundary() -> None:
    gate = RiskGate(make_settings(risk_max_order_qty=100), FakeRedis())
    await gate.check(make_order(quantity=100))  # == passes
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(quantity=101))
    assert exc.value.cap == "max_order_qty"


async def test_notional_cap_uses_price_then_stop_price() -> None:
    settings = make_settings(risk_max_order_value=Decimal("1000"))
    gate = RiskGate(settings, FakeRedis())
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(price="11", quantity=100))  # 1100 > 1000
    assert exc.value.cap == "max_order_value"
    with pytest.raises(RiskRejected):
        await gate.check(make_order(order_type="STOP", price=None, stop_price="11", quantity=100))
    await gate.check(make_order(price="10", quantity=100))  # 1000 == cap passes


async def test_unpriced_order_skips_notional_quantity_still_binds() -> None:
    settings = make_settings(risk_max_order_value=Decimal("1"), risk_max_order_qty=100)
    gate = RiskGate(settings, FakeRedis())
    await gate.check(make_order(order_type="MARKET", price=None, quantity=100))
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(order_type="MARKET", price=None, quantity=101))
    assert exc.value.cap == "max_order_qty"


async def test_rate_limit_nth_passes_n_plus_one_rejects() -> None:
    redis = FakeRedis()
    gate = RiskGate(make_settings(risk_max_orders_per_second=2), redis)
    await gate.check(make_order(symbol="A1"))
    await gate.check(make_order(symbol="A2"))
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(symbol="A3"))
    assert exc.value.cap == "rate_limit"


async def test_duplicate_burst_same_economic_order_different_ids() -> None:
    redis = FakeRedis()
    gate = RiskGate(make_settings(risk_max_orders_per_second=100), redis)
    await gate.check(make_order())
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order())  # fresh UUID, same (account,symbol,side,qty)
    assert exc.value.cap == "duplicate_burst"
    # the burst key is hashed — the raw account string never appears in Redis
    assert not any("ACC-TEST" in k for k in redis.store)


async def test_redis_down_fails_open_in_sim() -> None:
    redis = FakeRedis()
    redis.fail = True
    gate = RiskGate(make_settings(stage="sim"), redis)
    await gate.check(make_order())  # WARN + pass


async def test_redis_down_fails_closed_in_micro_live() -> None:
    redis = FakeRedis()
    redis.fail = True
    gate = RiskGate(make_settings(stage="micro_live"), redis)
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order())
    assert exc.value.cap == "risk_backend_down"


async def test_no_redis_client_follows_same_policy() -> None:
    await RiskGate(make_settings(stage="paper"), None).check(make_order())
    with pytest.raises(RiskRejected):
        await RiskGate(make_settings(stage="live"), None).check(make_order())

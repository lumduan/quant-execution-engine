"""PTRM gate: cap boundaries, throttles, stage-aware fail policy."""

from __future__ import annotations

from decimal import Decimal

import pytest
from src.quant_execution_engine.contracts.errors import DuplicateBurstDetected, RiskRejected
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
    with pytest.raises(DuplicateBurstDetected) as exc:  # 409, not 429 (A3 unified)
        await gate.check(make_order())  # fresh UUID, same fingerprint
    assert exc.value.code == "duplicate_burst_detected"
    assert exc.value.detail["window_seconds"] == 5  # the new window setting
    # the burst key is hashed — the raw account string never appears in Redis
    assert not any("ACC-TEST" in k for k in redis.store)


async def test_duplicate_burst_different_price_is_not_a_duplicate() -> None:
    """The richer fingerprint (+price) lets a legitimate re-price through."""
    redis = FakeRedis()
    gate = RiskGate(make_settings(risk_max_orders_per_second=100), redis)
    await gate.check(make_order(price="100"))
    await gate.check(make_order(price="101"))  # different price ⇒ different fingerprint


async def test_duplicate_burst_different_order_type_is_not_a_duplicate() -> None:
    redis = FakeRedis()
    gate = RiskGate(make_settings(risk_max_orders_per_second=100), redis)
    await gate.check(make_order(order_type="LIMIT", price="100"))
    # A MARKET order (price None) of the same qty is a distinct fingerprint.
    await gate.check(make_order(order_type="MARKET", price=None))


async def test_duplicate_burst_guard_disabled_passes() -> None:
    redis = FakeRedis()
    gate = RiskGate(
        make_settings(duplicate_burst_guard_enabled=False, risk_max_orders_per_second=100),
        redis,
    )
    await gate.check(make_order())
    await gate.check(make_order())  # exact duplicate passes — guard is off
    # No burst key was written (the guard never touched Redis for the fingerprint).
    assert not any(k.startswith("exe:burst:") for k in redis.store)


async def test_rate_cap_runs_even_with_burst_guard_disabled() -> None:
    """The per-second rate cap is independent of the burst guard."""
    redis = FakeRedis()  # a fresh redis so the rate counter starts clean
    gate = RiskGate(
        make_settings(duplicate_burst_guard_enabled=False, risk_max_orders_per_second=1),
        redis,
    )
    await gate.check(make_order(symbol="R1"))
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(symbol="R2"))
    assert exc.value.cap == "rate_limit"


# ----------------------------------------------------------- per-account caps


async def test_per_account_qty_cap_binds_when_present_even_in_sim() -> None:
    settings = make_settings(
        stage="sim",
        risk_max_order_qty=1000,  # generous global
        account_max_qty={"ACC-CAPPED": 50},
    )
    gate = RiskGate(settings, FakeRedis())
    await gate.check(make_order(account="ACC-CAPPED", quantity=50))  # == binds, passes
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(account="ACC-CAPPED", quantity=51))
    assert exc.value.cap == "account_max_qty"
    assert exc.value.detail["limit"] == 50


async def test_missing_account_falls_back_to_global_qty_cap() -> None:
    settings = make_settings(risk_max_order_qty=100, account_max_qty={"OTHER": 5})
    gate = RiskGate(settings, FakeRedis())
    await gate.check(make_order(account="UNLISTED", quantity=100))  # global cap binds
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(account="UNLISTED", quantity=101))
    assert exc.value.cap == "max_order_qty"  # global cap name, not the per-account one


async def test_per_account_notional_cap_binds_when_present() -> None:
    settings = make_settings(
        stage="sim",
        risk_max_order_value=Decimal("100000000"),  # generous global
        account_max_notional={"ACC-CAPPED": Decimal("1000")},
    )
    gate = RiskGate(settings, FakeRedis())
    await gate.check(make_order(account="ACC-CAPPED", price="10", quantity=100))  # 1000 == cap
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(account="ACC-CAPPED", price="11", quantity=100))  # 1100 > 1000
    assert exc.value.cap == "account_max_notional"
    assert exc.value.detail["limit"] == "1000"


async def test_missing_account_falls_back_to_global_notional_cap() -> None:
    settings = make_settings(
        risk_max_order_value=Decimal("1000"), account_max_notional={"OTHER": Decimal("5")}
    )
    gate = RiskGate(settings, FakeRedis())
    await gate.check(make_order(account="UNLISTED", price="10", quantity=100))  # 1000 == global
    with pytest.raises(RiskRejected) as exc:
        await gate.check(make_order(account="UNLISTED", price="11", quantity=100))
    assert exc.value.cap == "max_order_value"


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

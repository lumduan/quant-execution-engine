"""Pool/Redis singletons, single-flight lock, counters, settings, capabilities, stage."""

from __future__ import annotations

from typing import Any

import pytest
from src.quant_execution_engine.adapters import market_data
from src.quant_execution_engine.adapters.sim import SimAdapter
from src.quant_execution_engine.cache import redis_client
from src.quant_execution_engine.cache.counters import incr_with_ttl
from src.quant_execution_engine.cache.single_flight import single_flight
from src.quant_execution_engine.config.settings import Settings, get_settings
from src.quant_execution_engine.contracts import capabilities
from src.quant_execution_engine.contracts.enums import Broker, Market, Stage
from src.quant_execution_engine.contracts.errors import CapabilityError, StageRejected
from src.quant_execution_engine.core.stage import resolve_adapter
from src.quant_execution_engine.db import postgres
from src.quant_execution_engine.db.errors import PoolNotInitializedError, RepositoryError

from tests._fakes import FakeRedis
from tests.conftest import make_settings

# ------------------------------------------------------------------- postgres


async def test_pool_singleton_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class FakeAsyncpgPool:
        async def close(self) -> None:
            return None

    async def fake_create_pool(*, dsn: str, min_size: int, max_size: int) -> Any:
        created.append(dsn)
        return FakeAsyncpgPool()

    monkeypatch.setattr("asyncpg.create_pool", fake_create_pool)
    with pytest.raises(PoolNotInitializedError):
        postgres.get_pool()
    pool = await postgres.create_pool("dsn://x", min_size=1, max_size=2)
    assert postgres.get_pool() is pool
    assert await postgres.create_pool("dsn://other") is pool  # singleton
    assert created == ["dsn://x"]
    await postgres.close_pool()
    with pytest.raises(PoolNotInitializedError):
        postgres.get_pool()
    await postgres.close_pool()  # idempotent no-op


async def test_pool_creation_failure_maps_to_repository_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**_: Any) -> Any:
        raise OSError("nope")

    monkeypatch.setattr("asyncpg.create_pool", boom)
    with pytest.raises(RepositoryError):
        await postgres.create_pool("dsn://x")


# ---------------------------------------------------------------------- redis


async def test_redis_singleton_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda url, decode_responses: fake)
    assert redis_client.get_redis() is None
    client = redis_client.create_redis("redis://x")
    assert client is fake
    assert redis_client.create_redis("redis://other") is fake  # singleton
    assert redis_client.get_redis() is fake
    await redis_client.close_redis()
    assert redis_client.get_redis() is None
    await redis_client.close_redis()  # idempotent no-op


# -------------------------------------------------------------- single-flight


async def test_single_flight_acquire_and_release() -> None:
    redis = FakeRedis()
    async with single_flight(redis, "exe:submit:x", ttl_seconds=5) as acquired:
        assert acquired
        assert "exe:submit:x" in redis.store
        assert redis.ttls["exe:submit:x"] == 5
    assert "exe:submit:x" not in redis.store  # released


async def test_single_flight_lock_miss() -> None:
    redis = FakeRedis()
    await redis.set("exe:submit:x", "other")
    async with single_flight(redis, "exe:submit:x", ttl_seconds=5) as acquired:
        assert not acquired
    assert redis.store["exe:submit:x"] == "other"  # never deleted by a non-holder


async def test_single_flight_degrades_without_redis() -> None:
    async with single_flight(None, "k", ttl_seconds=5) as acquired:
        assert acquired  # PK is the backstop
    failing = FakeRedis()
    failing.fail = True
    async with single_flight(failing, "k", ttl_seconds=5) as acquired:
        assert acquired  # acquire failure -> proceed; release failure swallowed


async def test_incr_with_ttl_arms_once() -> None:
    redis = FakeRedis()
    assert await incr_with_ttl(redis, "k", 9) == 1
    assert redis.ttls["k"] == 9
    redis.ttls["k"] = 1  # pretend time passed; second incr must not re-arm
    assert await incr_with_ttl(redis, "k", 9) == 2
    assert redis.ttls["k"] == 1


# ------------------------------------------------------- market-data client (A2)


async def test_market_data_client_singleton_lifecycle() -> None:
    settings = make_settings(market_data_base_url="http://md:8000")
    assert market_data.get_market_data_client() is None
    client = market_data.create_market_data_client(settings)
    assert market_data.get_market_data_client() is client
    assert client.configured is True
    # Idempotent: a second create returns the same instance.
    assert market_data.create_market_data_client(settings) is client
    await market_data.close_market_data_client()
    assert market_data.get_market_data_client() is None
    await market_data.close_market_data_client()  # idempotent no-op


async def test_market_data_client_unconfigured_without_base_url() -> None:
    client = market_data.create_market_data_client(make_settings())
    assert client.configured is False
    # An unconfigured client never fetches — last_close short-circuits to None.
    from src.quant_execution_engine.contracts.enums import Market

    assert await client.last_close("PTT", Market.SET) is None
    await market_data.close_market_data_client()


# ------------------------------------------------------------------- settings


def test_settings_defaults_are_safe() -> None:
    settings = make_settings()
    assert settings.public_mode is True
    assert settings.stage is Stage.SIM
    assert settings.kill_switch_engaged is False
    assert settings.api_key is None
    assert settings.risk_max_order_qty > 0


def test_phase6_settings_defaults() -> None:
    """Phase 6 / A-foundation: account caps empty, price-band off, burst on @5s."""
    settings = make_settings()
    assert settings.account_max_notional == {}
    assert settings.account_max_qty == {}
    assert settings.price_band_enabled is False
    assert str(settings.price_band_max_pct) == "10.0"
    assert settings.duplicate_burst_guard_enabled is True  # default ON (Design §1)
    assert settings.duplicate_burst_window_seconds == 5


def test_settings_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXECUTION_ENGINE_STAGE", "paper")
    monkeypatch.setenv("EXECUTION_ENGINE_PUBLIC_MODE", "false")
    monkeypatch.setenv("EXECUTION_ENGINE_RISK_MAX_ORDER_VALUE", "5")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.stage is Stage.PAPER
    assert settings.public_mode is False
    assert str(settings.risk_max_order_value) == "5"
    monkeypatch.setenv("EXECUTION_ENGINE_STAGE", "yolo")
    with pytest.raises(Exception, match="stage"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


# --------------------------------------------------------------- capabilities


def test_capability_lookup_and_unsupported_market() -> None:
    row = capabilities.lookup(Broker.SIM, Market.SET)
    assert row.adapter_installed
    settrade_set = capabilities.lookup(Broker.SETTRADE, Market.SET)  # SET since Phase 4
    assert settrade_set.adapter_installed
    assert settrade_set.position_effects == ()


def test_capability_assert_supports_each_axis() -> None:
    from src.quant_execution_engine.contracts.enums import (
        OrderType,
        PositionEffect,
        Tif,
    )

    liberator_set = capabilities.lookup(Broker.LIBERATOR, Market.SET)
    with pytest.raises(CapabilityError, match="order_type"):
        liberator_set.assert_supports(OrderType.STOP, Tif.DAY, None)
    settrade = capabilities.lookup(Broker.SETTRADE, Market.TFEX)
    with pytest.raises(CapabilityError, match="order_type"):
        settrade.assert_supports(OrderType.ATC, Tif.DAY, PositionEffect.OPEN)  # no deriv ATC
    with pytest.raises(CapabilityError, match="position_effect"):
        liberator_set.assert_supports(OrderType.LIMIT, Tif.DAY, PositionEffect.OPEN)
    settrade.assert_supports(OrderType.LIMIT, Tif.GTC, PositionEffect.OPEN)  # GTC pinned P4


def test_matrix_shape() -> None:
    assert len(capabilities.CAPABILITY_MATRIX) == 6  # +SETTRADE×SET since Phase 4
    installed = {c.broker for c in capabilities.CAPABILITY_MATRIX if c.adapter_installed}
    assert installed == {Broker.SIM, Broker.LIBERATOR, Broker.SETTRADE}  # Settrade Phase 4


# ----------------------------------------------------------------------- stage


def test_stage_ladder_resolution() -> None:
    sim = SimAdapter(default_fill_price=make_settings().sim_default_fill_price)
    assert resolve_adapter(Stage.SIM, Broker.SIM, sim_adapter=sim) is sim
    assert resolve_adapter(Stage.PAPER, Broker.LIBERATOR, sim_adapter=sim) is sim
    for stage in (Stage.MICRO_LIVE, Stage.LIVE):
        with pytest.raises(StageRejected):
            resolve_adapter(stage, Broker.SIM, sim_adapter=sim)

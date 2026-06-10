"""Kill-switch state: env flag, runtime trip, pinning, degraded Redis."""

from __future__ import annotations

import pytest
from src.quant_execution_engine.cache.errors import CacheError
from src.quant_execution_engine.contracts.errors import (
    KillSwitchEngagedError,
    KillSwitchPinnedError,
)
from src.quant_execution_engine.core.kill_switch import KILL_SWITCH_KEY, KillSwitch

from tests._fakes import FakeRedis
from tests.conftest import make_settings


async def test_disengaged_by_default() -> None:
    switch = KillSwitch(make_settings(), FakeRedis())
    assert await switch.status() == (False, None)
    await switch.assert_disengaged()


async def test_env_flag_engages() -> None:
    switch = KillSwitch(make_settings(kill_switch_engaged=True), FakeRedis())
    assert await switch.status() == (True, "env")
    with pytest.raises(KillSwitchEngagedError):
        await switch.assert_disengaged()


async def test_runtime_trip_and_disengage() -> None:
    redis = FakeRedis()
    switch = KillSwitch(make_settings(), redis)
    await switch.engage()
    assert redis.store[KILL_SWITCH_KEY] == "engaged"
    assert await switch.status() == (True, "redis")
    await switch.disengage()
    assert await switch.status() == (False, None)


async def test_env_pin_wins_over_runtime_disengage() -> None:
    switch = KillSwitch(make_settings(kill_switch_engaged=True), FakeRedis())
    with pytest.raises(KillSwitchPinnedError):
        await switch.disengage()


async def test_redis_read_failure_falls_back_to_env_flag() -> None:
    redis = FakeRedis()
    redis.fail = True
    switch = KillSwitch(make_settings(), redis)
    assert await switch.status() == (False, None)  # WARN, env flag rules


async def test_engage_without_redis_raises_cache_error() -> None:
    switch = KillSwitch(make_settings(), None)
    with pytest.raises(CacheError):
        await switch.engage()
    with pytest.raises(CacheError):
        await switch.disengage()

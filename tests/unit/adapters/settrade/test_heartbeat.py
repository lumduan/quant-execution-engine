"""Heartbeat worker: breaker trip after N failures, single on_trip, healthy reset."""

from __future__ import annotations

import asyncio
import contextlib

import respx
from src.quant_execution_engine.adapters.session import BreakerState
from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
from src.quant_execution_engine.adapters.settrade.heartbeat import heartbeat_loop, heartbeat_pass

from tests.unit.adapters.settrade.test_adapter_place import _login_url, make_adapter


class TripRecorder:
    def __init__(self) -> None:
        self.calls = 0
        self.raise_once = False

    async def __call__(self) -> None:
        self.calls += 1
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("hook exploded")


_TOKEN = {
    "token_type": "Bearer",
    "access_token": "atk",
    "refresh_token": "rtk",
    "expires_in": 1800,
}


@respx.mock
async def test_breaker_trips_after_threshold_and_fires_on_trip_once() -> None:
    respx.post(_login_url()).respond(status_code=503)  # token unacquirable
    adapter = make_adapter(breaker_threshold=3)
    trip = TripRecorder()
    for expected_state in (BreakerState.CLOSED, BreakerState.CLOSED, BreakerState.OPEN):
        assert await heartbeat_pass(adapter, on_trip=trip) is False
        assert adapter.breaker.state is expected_state
    assert trip.calls == 1
    # Still OPEN on further failures — on_trip must NOT re-fire.
    await heartbeat_pass(adapter, on_trip=trip)
    assert adapter.breaker.state is BreakerState.OPEN
    assert trip.calls == 1
    await adapter.aclose()


@respx.mock
async def test_healthy_poll_resets_the_breaker() -> None:
    route = respx.post(_login_url())
    adapter = make_adapter(breaker_threshold=2)
    trip = TripRecorder()
    route.respond(status_code=503)
    await heartbeat_pass(adapter, on_trip=trip)
    await heartbeat_pass(adapter, on_trip=trip)
    assert adapter.breaker.state is BreakerState.OPEN
    route.respond(json=_TOKEN)
    assert await heartbeat_pass(adapter, on_trip=trip) is True
    state_after_reset: BreakerState = adapter.breaker.state
    assert state_after_reset is BreakerState.CLOSED
    # A fresh failure streak trips (and fires the hook) again.
    route.respond(status_code=503)
    # Force re-login each pass: the cached token would otherwise stay valid.
    await heartbeat_pass(_fresh_token_adapter(adapter), on_trip=trip)
    await heartbeat_pass(_fresh_token_adapter(adapter), on_trip=trip)
    assert trip.calls == 2
    await adapter.aclose()


def _fresh_token_adapter(adapter: SettradeAdapter) -> SettradeAdapter:
    """Invalidate the cached token so the next heartbeat re-acquires it."""
    adapter._client._access_token = None  # noqa: SLF001 - test hook
    return adapter


@respx.mock
async def test_intermittent_failures_below_threshold_never_trip() -> None:
    route = respx.post(_login_url())
    adapter = make_adapter(breaker_threshold=3)
    trip = TripRecorder()
    for _ in range(3):
        route.respond(status_code=503)
        _fresh_token_adapter(adapter)
        await heartbeat_pass(adapter, on_trip=trip)
        route.respond(json=_TOKEN)
        _fresh_token_adapter(adapter)
        await heartbeat_pass(adapter, on_trip=trip)
    assert adapter.breaker.state is BreakerState.CLOSED
    assert trip.calls == 0
    await adapter.aclose()


@respx.mock
async def test_on_trip_exception_does_not_propagate() -> None:
    respx.post(_login_url()).respond(status_code=503)
    adapter = make_adapter(breaker_threshold=1)
    trip = TripRecorder()
    trip.raise_once = True
    assert await heartbeat_pass(adapter, on_trip=trip) is False  # hook error swallowed
    assert adapter.breaker.state is BreakerState.OPEN
    assert trip.calls == 1
    await adapter.aclose()


@respx.mock
async def test_dead_wire_after_token_also_feeds_the_breaker() -> None:
    """A valid token but a failed last wire call is NOT healthy (last_wire_ok)."""
    respx.post(_login_url()).respond(json=_TOKEN)
    adapter = make_adapter(breaker_threshold=2)
    trip = TripRecorder()
    # Heartbeat acquires a token (last_wire_ok True) -> healthy.
    assert await heartbeat_pass(adapter, on_trip=trip) is True
    # Simulate a subsequent failed real wire call: ensure_token reuses the cached
    # token (no HTTP), but last_wire_ok is now False -> unhealthy.
    adapter._client.last_wire_ok = False  # noqa: SLF001 - test hook
    assert await heartbeat_pass(adapter, on_trip=trip) is False
    adapter._client.last_wire_ok = False  # noqa: SLF001 - test hook
    assert await heartbeat_pass(adapter, on_trip=trip) is False
    assert adapter.breaker.state is BreakerState.OPEN
    assert trip.calls == 1
    await adapter.aclose()


@respx.mock
async def test_heartbeat_loop_runs_and_cancels_cleanly() -> None:
    """The loop shell survives at least one pass and cancels without raising."""
    respx.post(_login_url()).respond(json=_TOKEN)
    adapter = make_adapter(breaker_threshold=3)
    trip = TripRecorder()
    task = asyncio.create_task(heartbeat_loop(adapter, interval_seconds=0, on_trip=trip))
    await asyncio.sleep(0)  # let it spin at least once
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await adapter.aclose()

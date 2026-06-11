"""Heartbeat worker: breaker trip after N failures, single on_trip, healthy reset."""

from __future__ import annotations

import respx
from src.quant_execution_engine.adapters.liberator.heartbeat import heartbeat_pass
from src.quant_execution_engine.adapters.session import BreakerState

from tests.unit.adapters.liberator.test_adapter_place import _BASE, make_adapter

_HEALTH = f"{_BASE}/order/health/set"
_HEALTHY = {"status": "healthy", "auth_token_available": True}


class TripRecorder:
    def __init__(self) -> None:
        self.calls = 0
        self.raise_once = False

    async def __call__(self) -> None:
        self.calls += 1
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("hook exploded")


@respx.mock
async def test_breaker_trips_after_threshold_and_fires_on_trip_once() -> None:
    respx.get(_HEALTH).respond(status_code=503)
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
    route = respx.get(_HEALTH)
    adapter = make_adapter(breaker_threshold=2)
    trip = TripRecorder()
    route.respond(status_code=503)
    await heartbeat_pass(adapter, on_trip=trip)
    await heartbeat_pass(adapter, on_trip=trip)
    assert adapter.breaker.state is BreakerState.OPEN
    route.respond(json=_HEALTHY)
    assert await heartbeat_pass(adapter, on_trip=trip) is True
    # Explicit annotation: mypy otherwise pins the property to the narrowed
    # Literal[OPEN] from the assert above (it cannot see the mutation).
    state_after_reset: BreakerState = adapter.breaker.state
    assert state_after_reset is BreakerState.CLOSED
    # A fresh failure streak trips (and fires the hook) again.
    route.respond(status_code=503)
    await heartbeat_pass(adapter, on_trip=trip)
    await heartbeat_pass(adapter, on_trip=trip)
    assert trip.calls == 2
    await adapter.aclose()


@respx.mock
async def test_intermittent_failures_below_threshold_never_trip() -> None:
    route = respx.get(_HEALTH)
    adapter = make_adapter(breaker_threshold=3)
    trip = TripRecorder()
    for _ in range(3):
        route.respond(status_code=503)
        await heartbeat_pass(adapter, on_trip=trip)
        route.respond(json=_HEALTHY)
        await heartbeat_pass(adapter, on_trip=trip)
    assert adapter.breaker.state is BreakerState.CLOSED
    assert trip.calls == 0
    await adapter.aclose()


@respx.mock
async def test_on_trip_exception_does_not_propagate() -> None:
    respx.get(_HEALTH).respond(status_code=503)
    adapter = make_adapter(breaker_threshold=1)
    trip = TripRecorder()
    trip.raise_once = True
    assert await heartbeat_pass(adapter, on_trip=trip) is False  # hook error swallowed
    assert adapter.breaker.state is BreakerState.OPEN
    assert trip.calls == 1
    await adapter.aclose()


@respx.mock
async def test_dead_session_with_http_200_also_feeds_the_breaker() -> None:
    """auth_token_available=False is a failure even though HTTP is fine."""
    respx.get(_HEALTH).respond(json={"status": "healthy", "auth_token_available": False})
    adapter = make_adapter(breaker_threshold=2)
    trip = TripRecorder()
    await heartbeat_pass(adapter, on_trip=trip)
    await heartbeat_pass(adapter, on_trip=trip)
    assert adapter.breaker.state is BreakerState.OPEN
    assert trip.calls == 1
    await adapter.aclose()

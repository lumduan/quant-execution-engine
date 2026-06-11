"""Heartbeat worker: breaker trip after N failures, single on_trip, healthy reset."""

from __future__ import annotations

import asyncio
import contextlib

import respx
from src.quant_execution_engine.adapters.session import BreakerState
from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
from src.quant_execution_engine.adapters.settrade.heartbeat import heartbeat_loop, heartbeat_pass
from src.quant_execution_engine.contracts.enums import Market

from tests.unit.adapters.settrade.test_adapter_place import (
    _DERIV_CODE,
    _EQUITY_CODE,
    _login_url,
    make_adapter,
    make_dual_adapter,
)


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
    adapter._clients[Market.SET]._access_token = None  # noqa: SLF001 - test hook
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
    adapter._clients[Market.SET].last_wire_ok = False  # noqa: SLF001 - test hook
    assert await heartbeat_pass(adapter, on_trip=trip) is False
    adapter._clients[Market.SET].last_wire_ok = False  # noqa: SLF001 - test hook
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


# --------------------------------------------- Phase 4.1 all-sessions heartbeat


@respx.mock
async def test_dual_one_dead_app_makes_heartbeat_false_and_trips_breaker() -> None:
    """Equity 200 + derivatives 503: aggregate False, per-market truth, one breaker."""
    respx.post(_login_url(_EQUITY_CODE)).respond(json=_TOKEN)
    respx.post(_login_url(_DERIV_CODE)).respond(status_code=503)  # derivatives app dead
    adapter = make_dual_adapter(breaker_threshold=2)
    trip = TripRecorder()
    assert await heartbeat_pass(adapter, on_trip=trip) is False
    assert adapter.last_heartbeat_by_market == {Market.SET: True, Market.TFEX: False}
    state_after_one: BreakerState = adapter.breaker.state
    assert state_after_one is BreakerState.CLOSED  # one failure, below threshold
    assert await heartbeat_pass(adapter, on_trip=trip) is False
    state_after_two: BreakerState = adapter.breaker.state
    assert state_after_two is BreakerState.OPEN  # E28: one dead app trips it
    assert trip.calls == 1  # fired exactly once on the CLOSED->OPEN transition
    await adapter.aclose()


@respx.mock
async def test_dual_both_healthy_is_true() -> None:
    respx.post(_login_url(_EQUITY_CODE)).respond(json=_TOKEN)
    respx.post(_login_url(_DERIV_CODE)).respond(json=_TOKEN)
    adapter = make_dual_adapter(breaker_threshold=2)
    trip = TripRecorder()
    assert await heartbeat_pass(adapter, on_trip=trip) is True
    assert adapter.last_heartbeat_by_market == {Market.SET: True, Market.TFEX: True}
    assert adapter.breaker.state is BreakerState.CLOSED
    await adapter.aclose()


@respx.mock
async def test_sandbox_single_client_probed_exactly_once_per_pass() -> None:
    """The shared client logs in once per pass (id-dedupe), not once per market."""
    login = respx.post(_login_url()).respond(json=_TOKEN)
    adapter = make_adapter(breaker_threshold=2)
    trip = TripRecorder()
    assert await heartbeat_pass(adapter, on_trip=trip) is True
    assert login.call_count == 1  # one shared session, one probe
    assert adapter.last_heartbeat_by_market == {Market.SET: True, Market.TFEX: True}
    await adapter.aclose()

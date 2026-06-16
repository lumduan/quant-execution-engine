"""Heartbeat: session/status probe feeds the breaker; trip fires on_trip once; recovery resets."""

from __future__ import annotations

import respx
from src.quant_execution_engine.adapters.session import BreakerState
from src.quant_execution_engine.adapters.streaming_pro.heartbeat import heartbeat_pass

from tests.unit.adapters.streaming_pro.test_adapter_place import _BASE, make_adapter


class _Trip:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


@respx.mock
async def test_breaker_trips_once_then_recovers() -> None:
    probe = respx.get(f"{_BASE}/session/status")
    adapter = make_adapter(breaker_threshold=2)
    trip = _Trip()

    probe.respond(json={"alive": False})
    assert await heartbeat_pass(adapter, on_trip=trip) is False  # failure 1
    assert adapter.breaker.state.name == BreakerState.CLOSED.name
    assert await heartbeat_pass(adapter, on_trip=trip) is False  # failure 2 -> trip
    assert adapter.breaker.state.name == BreakerState.OPEN.name
    assert trip.count == 1
    # A further failure does not re-fire the hook (only the CLOSED->OPEN transition does).
    await heartbeat_pass(adapter, on_trip=trip)
    assert trip.count == 1

    probe.respond(json={"alive": True})
    assert await heartbeat_pass(adapter, on_trip=trip) is True  # recovery resets the breaker
    assert adapter.breaker.state.name == BreakerState.CLOSED.name
    await adapter.aclose()


@respx.mock
async def test_heartbeat_transport_error_counts_as_failure() -> None:
    respx.get(f"{_BASE}/session/status").respond(status_code=503)
    adapter = make_adapter()
    assert await adapter.heartbeat() is False
    assert adapter.last_heartbeat_ok is False
    await adapter.aclose()

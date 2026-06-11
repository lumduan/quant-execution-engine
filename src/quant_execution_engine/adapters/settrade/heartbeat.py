"""Proactive session heartbeat driving the circuit breaker (Design Decision 6).

A ~30 s low-impact OAuth token-liveness probe: success resets the breaker,
consecutive failures at the threshold trip it OPEN, and the CLOSED→OPEN
*transition* fires ``on_trip`` exactly once (the runtime wires it to the
router's mass-cancel sweep). The loop shell is deliberately thin —
``heartbeat_pass`` is the tested unit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from src.quant_execution_engine.adapters.session import BreakerState
from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter

logger = logging.getLogger(__name__)

TripHook = Callable[[], Awaitable[None]]


async def heartbeat_pass(adapter: SettradeAdapter, *, on_trip: TripHook) -> bool:
    """One probe: feed the breaker; fire ``on_trip`` on the trip transition."""
    was_open = adapter.breaker.state is BreakerState.OPEN
    ok = await adapter.heartbeat()  # never raises (adapter contract)
    if ok:
        if was_open:
            logger.warning("settrade session recovered; circuit breaker resetting")
        adapter.breaker.record_success()
        return True
    adapter.breaker.record_failure()
    if adapter.breaker.state is BreakerState.OPEN and not was_open:
        logger.error("settrade circuit breaker TRIPPED — halting routing, mass-cancel attempted")
        try:
            await on_trip()
        except Exception:  # noqa: BLE001 - the heartbeat loop must survive the hook
            logger.exception("settrade breaker on_trip hook failed")
    return False


async def heartbeat_loop(
    adapter: SettradeAdapter,
    *,
    interval_seconds: int,
    on_trip: TripHook,
) -> None:
    """The background worker shell (started by the runtime when enabled)."""
    while True:  # pragma: no branch - cancelled via task.cancel()
        try:
            await heartbeat_pass(adapter, on_trip=on_trip)
        except Exception:  # noqa: BLE001 - defensive; the loop never dies
            logger.exception("settrade heartbeat pass failed unexpectedly")
        await asyncio.sleep(interval_seconds)

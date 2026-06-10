"""Session circuit-breaker scaffolding (ADR §G — design pinned Phase 0).

Wired in Phase 2, inert for Sim: every adapter owns a breaker the router
guards on before venue I/O. The ~30 s proactive heartbeat worker that drives
``record_success``/``record_failure`` lands with the real adapters
(Phases 3/4); ``HeartbeatHook`` is the protocol they implement.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from src.quant_execution_engine.adapters.errors import CircuitOpenError


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class SessionCircuitBreaker:
    """Consecutive-failure breaker; OPEN halts new routing for the broker."""

    def __init__(self, *, failure_threshold: int = 3) -> None:
        self._failure_threshold = failure_threshold
        self._consecutive_failures = 0
        self._state: BreakerState = BreakerState.CLOSED

    @property
    def state(self) -> BreakerState:
        return self._state

    def record_success(self) -> None:
        """A healthy heartbeat/venue call resets the breaker."""
        self._consecutive_failures = 0
        self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        """Consecutive failures at the threshold trip the breaker OPEN."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._state = BreakerState.OPEN

    def guard(self) -> None:
        """Raise when OPEN — checked by the router before place/cancel."""
        if self._state is BreakerState.OPEN:
            raise CircuitOpenError("broker session circuit breaker is open")


class HeartbeatHook(Protocol):
    """Adapters implement a low-impact liveness probe (e.g. account read)."""

    async def heartbeat(self) -> bool:
        """Return True when the broker session is healthy."""
        ...

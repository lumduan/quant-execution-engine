"""Circuit-breaker scaffolding (inert for sim, unit-tested for Phases 3/4)."""

from __future__ import annotations

import pytest
from src.quant_execution_engine.adapters.errors import CircuitOpenError
from src.quant_execution_engine.adapters.session import BreakerState, SessionCircuitBreaker


def test_closed_by_default_and_guard_passes() -> None:
    breaker = SessionCircuitBreaker()
    assert breaker.state is BreakerState.CLOSED
    breaker.guard()


def test_opens_at_threshold_and_guard_raises() -> None:
    breaker = SessionCircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    after_two: BreakerState = breaker.state
    assert after_two is BreakerState.CLOSED
    breaker.record_failure()
    after_three: BreakerState = breaker.state
    assert after_three is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.guard()


def test_success_resets_consecutive_failures() -> None:
    breaker = SessionCircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    mid: BreakerState = breaker.state
    assert mid is BreakerState.CLOSED
    breaker.record_failure()
    tripped: BreakerState = breaker.state
    assert tripped is BreakerState.OPEN
    breaker.record_success()
    reset: BreakerState = breaker.state
    assert reset is BreakerState.CLOSED
    breaker.guard()

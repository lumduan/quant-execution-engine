"""/health `brokers` must cover every REAL broker, not just liberator (TK-0388).

The bug this guards: `_broker_runtime_health()` checked only the liberator
adapter, so a live, authenticated Streaming Pro session reported
``"brokers": null`` — the SAME reading as "no broker configured". Measured on the
AWS node 2026-08-21 at STAGE=paper with the runtime up and heartbeating.

⚠️ A test asserting merely ``brokers is not None`` would have PASSED throughout,
because the liberator entry alone satisfies it. The assertions here are keyed on
the ``Broker`` enum instead, so the next adapter added without a health entry
fails rather than repeats this silently.
"""

from __future__ import annotations

from src.quant_execution_engine.adapters import session as session_mod
from src.quant_execution_engine.adapters.liberator import runtime as liberator_runtime
from src.quant_execution_engine.adapters.streaming_pro import runtime as sp_runtime
from src.quant_execution_engine.api.routes import _broker_runtime_health
from src.quant_execution_engine.contracts.enums import Broker


class _StubRuntime:
    """Minimal stand-in exposing exactly what the health builder reads."""

    def __init__(self, *, healthy: bool | None = True) -> None:
        self.breaker = session_mod.SessionCircuitBreaker(failure_threshold=3)
        self.last_heartbeat_ok = healthy


def _real_brokers() -> set[str]:
    """Every broker that has a venue runtime — i.e. the enum minus `sim`.

    DERIVED from `Broker` rather than hardcoded: adding a member forces this to
    move, which is the point.
    """
    return {b.value for b in Broker if b is not Broker.SIM}


def test_broker_free_reports_none() -> None:
    """No runtime configured -> None. This is the `sim` case."""
    assert _broker_runtime_health() is None


def test_streaming_pro_appears_when_its_runtime_is_configured() -> None:
    """The regression: a live SP runtime must be visible on /health."""
    sp_runtime._adapter = _StubRuntime(healthy=True)  # type: ignore[assignment]

    brokers = _broker_runtime_health()
    assert brokers is not None
    assert "streaming_pro" in brokers
    assert brokers["streaming_pro"].breaker_state == "closed"
    assert brokers["streaming_pro"].session_healthy is True

    # Negative control: liberator is NOT configured here, so its absence proves
    # the entry above came from the SP branch and not from the pre-existing one.
    assert "liberator" not in brokers


def test_liberator_still_reported_and_is_independent() -> None:
    """The pre-existing branch is untouched by the fix."""
    liberator_runtime._adapter = _StubRuntime(healthy=False)  # type: ignore[assignment]

    brokers = _broker_runtime_health()
    assert brokers is not None
    assert brokers["liberator"].session_healthy is False
    assert "streaming_pro" not in brokers


def test_health_covers_EVERY_real_broker_in_the_enum() -> None:
    """The structural guard — this is what makes the next omission fail loudly.

    With every real broker's runtime configured, the emitted key set must equal
    the enum's real brokers exactly. A new `Broker` member added without a
    corresponding health branch turns this red; a test hardcoding
    ``{"liberator", "streaming_pro"}`` would not.
    """
    liberator_runtime._adapter = _StubRuntime()  # type: ignore[assignment]
    sp_runtime._adapter = _StubRuntime()  # type: ignore[assignment]

    brokers = _broker_runtime_health()
    assert brokers is not None
    assert set(brokers) == _real_brokers()

    # Positive control: the comparison is vacuous if the expected set is empty
    # or accidentally just `sim`.
    assert _real_brokers() == {"liberator", "streaming_pro"}
    assert "sim" not in brokers, "sim has no venue runtime and must never appear"

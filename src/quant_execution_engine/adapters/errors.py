"""Adapter-layer errors."""

from __future__ import annotations

from typing import ClassVar

from src.quant_execution_engine.contracts.errors import OrderRejectedError
from src.quant_execution_engine.errors import ExecutionEngineError


class AdapterError(ExecutionEngineError):
    """Unexpected adapter failure."""


class CircuitOpenError(OrderRejectedError):
    """The adapter's session circuit breaker is OPEN — routing halted (§G).

    Wire code pinned ``broker_circuit_open`` by the Phase 3 spec (renamed from
    the Phase-2 placeholder ``broker_session_down``; nothing consumed it).
    """

    code: ClassVar[str] = "broker_circuit_open"

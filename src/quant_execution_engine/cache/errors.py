"""Cache-layer errors."""

from __future__ import annotations

from src.quant_execution_engine.errors import ExecutionEngineError


class CacheError(ExecutionEngineError):
    """Unexpected Redis failure."""

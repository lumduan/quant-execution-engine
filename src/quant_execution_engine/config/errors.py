"""Configuration errors."""

from __future__ import annotations

from src.quant_execution_engine.errors import ExecutionEngineError


class ConfigError(ExecutionEngineError):
    """Invalid or missing configuration."""

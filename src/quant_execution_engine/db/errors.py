"""Database-layer errors."""

from __future__ import annotations

from src.quant_execution_engine.errors import ExecutionEngineError


class RepositoryError(ExecutionEngineError):
    """Unexpected database failure."""


class PoolNotInitializedError(RepositoryError):
    """The asyncpg pool was used before ``create_pool``."""


class DuplicateOrderSignal(ExecutionEngineError):
    """Internal: the orders PK collided — the durable dedupe backstop fired.

    Never crosses the API: the router catches it and returns the prior result
    (a duplicate submit is NOT an error, ADR §A).
    """

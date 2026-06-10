"""Module-level asyncpg pool (created in the app lifespan, closed on shutdown).

Mirrors the quant-marketdata-engine pattern verbatim: a process-wide singleton
so repositories never manage connections themselves.
"""

from __future__ import annotations

import logging

import asyncpg

from src.quant_execution_engine.db.errors import PoolNotInitializedError, RepositoryError

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    """Create (or return the existing) module-level asyncpg pool."""
    global _pool
    if _pool is not None:
        return _pool
    try:
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)
    except Exception as exc:
        raise RepositoryError(f"failed to create asyncpg pool: {exc}") from exc
    logger.info("asyncpg pool created (min=%d max=%d)", min_size, max_size)
    return _pool


def get_pool() -> asyncpg.Pool:
    """Return the initialized pool, or raise if ``create_pool`` was not called."""
    if _pool is None:
        raise PoolNotInitializedError("asyncpg pool is not initialized; call create_pool first")
    return _pool


async def close_pool() -> None:
    """Close and clear the module-level pool (no-op if uninitialized)."""
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("asyncpg pool closed")

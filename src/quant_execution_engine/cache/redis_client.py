"""Module-level redis.asyncio client (created in the lifespan, closed on shutdown).

Mirrors the quant-marketdata-engine pattern. The client object connects
lazily, so startup never blocks on Redis; individual operations may raise at
call time and callers degrade per the stage-aware fail policy.
"""

from __future__ import annotations

import logging
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger(__name__)

_client: Any | None = None


def create_redis(url: str) -> Any:
    """Create (or return the existing) module-level Redis client."""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(url, decode_responses=True)
        logger.info("redis client created")
    return _client


def get_redis() -> Any | None:
    """Return the client if created (None before the lifespan ran)."""
    return _client


async def close_redis() -> None:
    """Close and clear the module-level client (no-op if uninitialized)."""
    global _client
    if _client is None:
        return
    await _client.aclose()
    _client = None
    logger.info("redis client closed")

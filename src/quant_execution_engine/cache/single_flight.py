"""Single-flight submit lock (``SET NX EX`` + atomic compare-and-delete release).

The lock is contention politeness only — the ``execution.orders`` PRIMARY KEY
is the correctness backstop. Redis being unavailable therefore yields the lock
trivially (``acquired=True``) and lets the PK arbitrate the race.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


@asynccontextmanager
async def single_flight(client: Any | None, key: str, *, ttl_seconds: int) -> AsyncIterator[bool]:
    """Yield True when this caller holds the lock for ``key``.

    Holder releases atomically (compare-and-delete on a per-acquire token) so
    an expired lock taken over by another submit is never deleted by us.
    """
    token = uuid.uuid4().hex
    acquired = True
    if client is not None:
        try:
            acquired = bool(await client.set(key, token, nx=True, ex=ttl_seconds))
        except Exception:  # noqa: BLE001 - degrade to the PK backstop
            logger.warning("single-flight lock unavailable for %s; relying on PK", key)
            acquired = True
    try:
        yield acquired
    finally:
        if acquired and client is not None:
            try:
                await client.eval(_RELEASE_LUA, 1, key, token)
            except Exception:  # noqa: BLE001 - lock expires via TTL anyway
                logger.warning("single-flight release failed for %s (TTL will expire)", key)

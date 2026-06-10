"""Windowed counters for the PTRM rate / duplicate-burst caps."""

from __future__ import annotations

from typing import Any


async def incr_with_ttl(client: Any, key: str, ttl_seconds: int) -> int:
    """INCR the key; arm its TTL on first increment. Returns the new count."""
    count = int(await client.incr(key))
    if count == 1:
        await client.expire(key, ttl_seconds)
    return count

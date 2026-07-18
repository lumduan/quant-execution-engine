"""``TokenBucket`` — the venue-facing rate limiter (Phase 6 / D, Design Decision §7).

A minimal, pure-asyncio token bucket shared by the real-broker adapters: the
Liberator placement bucket (D2) and the Streaming Pro POST bucket. The prompt
forbids a third-party rate-limit library, so this is the one primitive; it is
deliberately small and deterministic.

Design invariants (the limiter must never harm the order path):

* **Lazy refill on a monotonic clock.** Tokens regenerate from elapsed time on
  every :meth:`acquire`; there is no background task to schedule or cancel.
* **Await-on-deficit, never busy-spin.** When no token is available the caller
  ``await``s the computed deficit via the injected ``sleep`` (``asyncio.sleep``
  in production), then consumes. The wait serialises bursts — that IS the
  back-pressure.
* **Never drops, never raises into the caller.** Throttling is invisible to the
  caller beyond the added latency; a rate-limited request still goes through.
* **One WARN per wait.** Exactly one ``<name>_rate_limit_wait`` log line per
  enforced wait, carrying the wait duration — never per token, never a spin.
* **``rate <= 0`` ⇒ disabled.** An unlimited bucket so a ``0`` setting can never
  deadlock the submit path (``acquire`` is a no-op).

The clock + sleep are injectable so tests are fully deterministic (a fake clock
+ a recording sleep prove the wait maths without real time passing).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class TokenBucket:
    """A single rate bucket: capacity = ``rate_per_second`` (a 1-second burst).

    One bucket guards one venue request class (e.g. Liberator place, Streaming
    Pro POST). Acquire one token before each outbound call; the bucket refills
    lazily at ``rate_per_second`` tokens/second up to its capacity.
    """

    def __init__(
        self,
        rate_per_second: float,
        *,
        name: str = "rate",
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rate = rate_per_second
        self._name = name
        self._now = now
        self._sleep = sleep
        # Capacity is the rate (a 1-second burst); start full so the first
        # ``rate`` acquires are immediate.
        self._capacity = rate_per_second
        self._tokens = rate_per_second
        self._updated = now()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """``False`` when ``rate <= 0`` (the bucket is a no-op / unlimited)."""
        return self._rate > 0

    def _refill_locked(self) -> None:
        """Add tokens for the elapsed monotonic time, capped at capacity."""
        timestamp = self._now()
        elapsed = timestamp - self._updated
        self._updated = timestamp
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    async def acquire(self) -> None:
        """Consume one token, awaiting the deficit if the bucket is empty.

        Fast path: a disabled bucket (``rate <= 0``) returns immediately. Else,
        under the lock, refill from elapsed time; if a whole token is available
        consume and return; otherwise compute the wait until the next token,
        emit ONE WARN, ``await`` the wait, then consume. Never busy-spins, never
        drops, never raises a throttle to the caller.
        """
        if not self.enabled:
            return
        async with self._lock:
            self._refill_locked()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Deficit: time until one more token accrues at ``rate`` tokens/sec.
            wait = (1.0 - self._tokens) / self._rate
            logger.warning("%s_rate_limit_wait wait=%.3fs", self._name, wait)
            await self._sleep(wait)
            # The sleep regenerated exactly the missing fraction; account for it
            # via the same lazy-refill path so a slow injected clock and a real
            # clock agree, then consume the token this caller waited for.
            self._refill_locked()
            self._tokens = max(0.0, self._tokens - 1.0)

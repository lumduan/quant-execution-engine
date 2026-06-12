"""The in-memory order-book cache + stream fan-out (Phase 5).

``OrderBookService`` is the single consumer-facing surface: providers call
:meth:`publish` from the event loop, snapshot/SimAdapter readers call :meth:`get`
(pure cache read — NEVER auto-subscribes), and SSE consumers iterate
:meth:`stream` (which holds a refcounted subscription so the router only talks to
a venue while at least one consumer wants the symbol).

Cache discipline (ADR D17): an in-memory LRU keyed ``(symbol, market)``, bounded
by ``max_symbols`` (least-recently-used by read-or-write is evicted) and
``max_age_seconds`` (a stale entry reads as absent). A dropped tick is a
resubscribe, never a loss — the cache is lossy-tolerant by design.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import OrderBook
from src.quant_execution_engine.order_book.router import ProviderRouter

logger = logging.getLogger(__name__)

_Key = tuple[str, Market]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Subscribers:
    """Per-key set of bounded queues with a refcount for the router."""

    __slots__ = ("queues",)

    def __init__(self) -> None:
        self.queues: list[asyncio.Queue[OrderBook]] = []


class OrderBookService:
    """In-memory LRU cache + per-(symbol, market) SSE fan-out."""

    def __init__(
        self,
        *,
        router: ProviderRouter,
        max_symbols: int,
        max_age_seconds: float,
        subscriber_queue_size: int,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._router = router
        self._max_symbols = max_symbols
        self._max_age_seconds = max_age_seconds
        self._subscriber_queue_size = subscriber_queue_size
        self._now = now
        self._cache: OrderedDict[_Key, OrderBook] = OrderedDict()
        self._subscribers: dict[_Key, _Subscribers] = {}
        self._refcounts: dict[_Key, int] = {}

    # ----------------------------------------------------------------- cache

    def publish(self, book: OrderBook) -> None:
        """Update the cache and fan out to subscribers (called on the loop)."""
        key: _Key = (book.symbol, book.market)
        self._cache[key] = book
        self._cache.move_to_end(key)
        self._evict()
        subs = self._subscribers.get(key)
        if subs is None:
            return
        for queue in subs.queues:
            try:
                queue.put_nowait(book)
            except asyncio.QueueFull:
                _drop_oldest(queue)
                queue.put_nowait(book)
                logger.warning(
                    "order_book.subscriber_lagged symbol=%s market=%s",
                    book.symbol,
                    book.market.value,
                )

    def get(self, symbol: str, market: Market) -> OrderBook | None:
        """Fresh-only read; a stale entry reads as absent. No auto-subscribe."""
        key: _Key = (symbol, market)
        book = self._cache.get(key)
        if book is None:
            return None
        age = (self._now() - book.received_at).total_seconds()
        if age > self._max_age_seconds:
            return None
        self._cache.move_to_end(key)
        return book

    def _evict(self) -> None:
        while len(self._cache) > self._max_symbols:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.debug(
                "order_book.cache_evict symbol=%s market=%s",
                evicted_key[0],
                evicted_key[1].value,
            )

    # ---------------------------------------------------------------- stream

    async def stream(self, symbol: str, market: Market) -> AsyncIterator[OrderBook]:
        """Yield books for ``(symbol, market)`` until the consumer stops.

        Entering acquires a refcounted subscription (0→1 ⇒ router.subscribe);
        the ``finally`` releases it (1→0 ⇒ router.unsubscribe).
        """
        queue: asyncio.Queue[OrderBook] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        await self._acquire(symbol, market, queue)
        try:
            while True:
                yield await queue.get()
        finally:
            await self._release(symbol, market, queue)

    async def _acquire(self, symbol: str, market: Market, queue: asyncio.Queue[OrderBook]) -> None:
        key: _Key = (symbol, market)
        subs = self._subscribers.setdefault(key, _Subscribers())
        subs.queues.append(queue)
        count = self._refcounts.get(key, 0) + 1
        self._refcounts[key] = count
        logger.info(
            "order_book.subscribers symbol=%s market=%s count=%d",
            symbol,
            market.value,
            count,
        )
        if count == 1:
            await self._router.subscribe(symbol, market)

    async def _release(self, symbol: str, market: Market, queue: asyncio.Queue[OrderBook]) -> None:
        key: _Key = (symbol, market)
        subs = self._subscribers.get(key)
        if subs is not None and queue in subs.queues:
            subs.queues.remove(queue)
        count = max(self._refcounts.get(key, 0) - 1, 0)
        if count == 0:
            self._refcounts.pop(key, None)
            self._subscribers.pop(key, None)
            await self._router.unsubscribe(symbol, market)
        else:
            self._refcounts[key] = count
        logger.info(
            "order_book.subscribers symbol=%s market=%s count=%d",
            symbol,
            market.value,
            count,
        )


def _drop_oldest(queue: asyncio.Queue[OrderBook]) -> None:
    """Discard the oldest queued book to make room for a newer one."""
    with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover - racy edge
        queue.get_nowait()

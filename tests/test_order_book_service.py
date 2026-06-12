"""OrderBookService tests: cache freshness, LRU, stream fan-out, refcounts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import (
    OrderBook,
    OrderBookLevel,
    OrderBookSource,
)
from src.quant_execution_engine.order_book.service import OrderBookService


class FakeRouter:
    """Records subscribe/unsubscribe so refcount transitions are observable."""

    def __init__(self) -> None:
        self.subscribed: list[tuple[str, Market]] = []
        self.unsubscribed: list[tuple[str, Market]] = []

    async def subscribe(self, symbol: str, market: Market) -> None:
        self.subscribed.append((symbol, market))

    async def unsubscribe(self, symbol: str, market: Market) -> None:
        self.unsubscribed.append((symbol, market))


class Clock:
    """An injectable clock for staleness tests."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class StreamConsumer:
    """Drives a ``stream()`` generator in a task, collecting yielded books.

    Entering primes the generator past its first ``yield`` (so the refcounted
    subscribe has fired); ``aclose`` stops it (so the release/unsubscribe fires).
    """

    def __init__(self, gen: AsyncIterator[OrderBook]) -> None:
        self._gen = gen
        self.received: list[OrderBook] = []
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> StreamConsumer:
        started = asyncio.Event()

        async def _run() -> None:
            agen = self._gen
            # Manually enter so the subscribe runs before we report "started".
            it = agen.__aiter__()
            first = asyncio.ensure_future(it.__anext__())
            await asyncio.sleep(0)
            started.set()
            try:
                self.received.append(await first)
                async for book in agen:
                    self.received.append(book)
            except (StopAsyncIteration, asyncio.CancelledError):
                return

        self._task = asyncio.create_task(_run())
        await started.wait()
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        aclose = getattr(self._gen, "aclose", None)
        if aclose is not None:
            await aclose()


def _book(symbol: str = "AOT", *, received_at: datetime | None = None, seq: int = 1) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        market=Market.SET,
        bid_levels=[OrderBookLevel(price=Decimal("837.8"), volume=26)],
        ask_levels=[OrderBookLevel(price=Decimal("838"), volume=24)],
        sequence=seq,
        source=OrderBookSource.SETTRADE,
        received_at=received_at or datetime.now(UTC),
    )


def _service(router: FakeRouter, **overrides: object) -> OrderBookService:
    kwargs: dict[str, object] = {
        "router": router,
        "max_symbols": 500,
        "max_age_seconds": 5,
        "subscriber_queue_size": 256,
    }
    kwargs.update(overrides)
    return OrderBookService(**kwargs)  # type: ignore[arg-type]


async def test_publish_then_get_is_fresh() -> None:
    service = _service(FakeRouter())
    book = _book()
    service.publish(book)
    assert service.get("AOT", Market.SET) == book


async def test_get_unknown_is_none() -> None:
    service = _service(FakeRouter())
    assert service.get("NOPE", Market.SET) is None


async def test_stale_entry_reads_as_absent() -> None:
    start = datetime(2026, 6, 12, tzinfo=UTC)
    clock = Clock(start)
    service = _service(FakeRouter(), max_age_seconds=5, now=clock)
    service.publish(_book(received_at=start))
    clock.advance(4)
    assert service.get("AOT", Market.SET) is not None
    clock.advance(2)  # now 6s old > 5s bound
    assert service.get("AOT", Market.SET) is None


async def test_lru_evicts_at_max_symbols() -> None:
    service = _service(FakeRouter(), max_symbols=2)
    service.publish(_book("AAA"))
    service.publish(_book("BBB"))
    service.get("AAA", Market.SET)  # touch AAA so BBB is the LRU
    service.publish(_book("CCC"))  # evicts BBB
    assert service.get("AAA", Market.SET) is not None
    assert service.get("CCC", Market.SET) is not None
    assert service.get("BBB", Market.SET) is None


async def test_stream_receives_published_books() -> None:
    service = _service(FakeRouter())
    async with StreamConsumer(service.stream("AOT", Market.SET)) as consumer:
        service.publish(_book(seq=42))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert any(b.sequence == 42 for b in consumer.received)


async def test_first_subscriber_triggers_router_subscribe_once() -> None:
    router = FakeRouter()
    service = _service(router)
    async with StreamConsumer(service.stream("AOT", Market.SET)):
        async with StreamConsumer(service.stream("AOT", Market.SET)):
            assert router.subscribed == [("AOT", Market.SET)]  # only the first
        assert router.unsubscribed == []  # still one consumer
    assert router.unsubscribed == [("AOT", Market.SET)]  # last exit


async def test_slow_subscriber_drops_oldest_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router = FakeRouter()
    service = _service(router, subscriber_queue_size=1)
    async with StreamConsumer(service.stream("AOT", Market.SET)):
        # The consumer is parked on its first __anext__; the queue holds 1.
        with caplog.at_level(logging.WARNING):
            service.publish(_book(seq=1))
            service.publish(_book(seq=2))  # overflow -> drop-oldest + warn
        assert "order_book.subscriber_lagged" in caplog.text

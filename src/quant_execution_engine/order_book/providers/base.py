"""The provider protocol every order-book feed implements (Phase 5).

A provider owns one venue's realtime bid/offer connection. The router calls
``subscribe``/``unsubscribe`` for a ``(symbol, market)`` as stream consumers come
and go; the provider bridges raw venue ticks onto the event loop via the
``on_book`` callback (which MUST be invoked on the loop) and signals failures via
``on_error`` (the failover signal — a free-form reason string). Providers never
touch the cache directly; they only emit normalized
:class:`~src.quant_execution_engine.order_book.models.OrderBook` objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar

from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import OrderBook, OrderBookSource

OnBook = Callable[[OrderBook], None]
OnError = Callable[[OrderBookSource, str], None]


class OrderBookProvider(ABC):
    """One venue's realtime order-book feed."""

    name: ClassVar[OrderBookSource]

    def __init__(self, *, on_book: OnBook, on_error: OnError) -> None:
        self._on_book = on_book
        self._on_error = on_error

    @abstractmethod
    async def start(self) -> None:
        """Capture the running loop and prepare connections (idempotent)."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down all connections and background tasks (idempotent)."""

    @abstractmethod
    async def subscribe(self, symbol: str, market: Market) -> None:
        """Begin streaming ``(symbol, market)`` (the first consumer arrived)."""

    @abstractmethod
    async def unsubscribe(self, symbol: str, market: Market) -> None:
        """Stop streaming ``(symbol, market)`` (the last consumer left)."""

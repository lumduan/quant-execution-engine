"""``SimFillPricer`` — the D21 fill-price chain for the ``SimAdapter``.

Resolves a paper-fill price for a :class:`NormalizedOrder` in three hops, each
logging clearly so the active source is always visible:

1. **Order-book cache** (pure read, never subscribes): BUY → best ask, SELL →
   best bid, bounded by the order's own limit (a fill never crosses its limit).
2. **Market-data engine last close** (only when a base URL is configured): the
   ``GET /ohlcv`` ``SET:``/``TFEX:``-prefixed last ``1d`` bar's ``close``, also
   limit-bounded. ANY failure falls through.
3. ``None`` — the ``SimAdapter`` then uses its own ``_reference_price``.

Owns its lazily-created ``httpx.AsyncClient`` when one is not injected; the
market-data API key is a ``SecretStr`` and is NEVER logged. This module imports
``order_book`` (the dependency arrow points one way — ``SimAdapter`` depends on a
small Protocol, not on this module or on ``order_book``).
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
from pydantic import SecretStr

from src.quant_execution_engine.adapters.market_data import MarketDataClient
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Side
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.order_book.runtime import get_order_book_service
from src.quant_execution_engine.order_book.service import OrderBookService

logger = logging.getLogger(__name__)


def _bound_by_limit(price: Decimal, order: NormalizedOrder) -> Decimal:
    """Clamp a candidate fill price to never cross the order's own limit.

    Price-only: the fill PLAN is untouched. BUY fills at ``min(price, limit)``,
    SELL at ``max(price, limit)``. A market order (no ``price``) is unbounded.
    """
    if order.price is None:
        return price
    if order.side is Side.BUY:
        return min(price, order.price)
    return max(price, order.price)


class SimFillPricer:
    """Resolve a sim fill price via the D21 chain (book → marketdata → None)."""

    def __init__(
        self,
        order_book: OrderBookService | None,
        market_data_base_url: str | None,
        market_data_api_key: SecretStr | None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._order_book = order_book
        self._market_data = MarketDataClient(market_data_base_url, market_data_api_key, http)

    async def aclose(self) -> None:
        """Close the owned market-data client (a no-op when one was injected)."""
        await self._market_data.aclose()

    async def fill_price(self, order: NormalizedOrder) -> Decimal | None:
        """The chain: order-book cache → market-data engine → ``None``."""
        book_price = self._from_book(order)
        if book_price is not None:
            return book_price
        if self._market_data.configured:
            md_price = await self._from_market_data(order)
            if md_price is not None:
                return md_price
        if self._order_book is not None or self._market_data.configured:
            # Only worth a line when there WAS a configured source to miss —
            # a bare-sim deployment (no book, no marketdata) stays quiet.
            logger.info("sim_pricing.reference_fallback symbol=%s", order.symbol)
        return None

    def _from_book(self, order: NormalizedOrder) -> Decimal | None:
        """Hop 1: best ask (BUY) / best bid (SELL) from the cache, limit-bounded."""
        if self._order_book is None:
            return None
        book = self._order_book.get(order.symbol, order.market)
        if book is None:
            return None
        level = book.best_ask if order.side is Side.BUY else book.best_bid
        if level is None:
            return None
        price = _bound_by_limit(level.price, order)
        logger.debug(
            "sim_pricing.book_hit symbol=%s side=%s price=%s",
            order.symbol,
            order.side.value,
            price,
        )
        return price

    async def _from_market_data(self, order: NormalizedOrder) -> Decimal | None:
        """Hop 2: the market-data engine's last ``1d`` close, limit-bounded."""
        close = await self._market_data.last_close(order.symbol, order.market)
        if close is None:
            return None
        price = _bound_by_limit(close, order)
        logger.info("sim_pricing.market_data_fallback symbol=%s price=%s", order.symbol, price)
        return price


_pricer: SimFillPricer | None = None


def create_sim_pricer(settings: Settings) -> SimFillPricer:
    """Create (or return) the process singleton pricer."""
    global _pricer
    if _pricer is None:
        _pricer = SimFillPricer(
            order_book=get_order_book_service(),
            market_data_base_url=settings.market_data_base_url,
            market_data_api_key=settings.market_data_api_key,
        )
    return _pricer


def get_sim_pricer() -> SimFillPricer | None:
    """The singleton pricer, or ``None`` before the lifespan created it."""
    return _pricer


async def close_sim_pricer() -> None:
    """Close + clear the singleton pricer (idempotent)."""
    global _pricer
    if _pricer is not None:
        await _pricer.aclose()
    _pricer = None


__all__ = [
    "SimFillPricer",
    "close_sim_pricer",
    "create_sim_pricer",
    "get_sim_pricer",
]

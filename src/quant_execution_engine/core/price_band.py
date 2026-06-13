"""Advisory price-band pre-trade check (Phase 6 / A2).

A LIMIT order whose price deviates from the symbol's last close by more than
``price_band_max_pct`` is rejected with a typed :class:`PriceBandExceeded` (422)
BEFORE the order reaches a venue. The check is deliberately advisory: it is a
no-op unless explicitly enabled AND a market-data base URL is configured, MARKET
orders bypass it (no limit to validate), and a market-data fetch failure logs a
WARNING and PASSES the order through (a degraded market-data hop must never
block routing). All prices are ``Decimal``; ``last_close`` is fetched via the
shared :class:`MarketDataClient` (the same hop the SimAdapter uses).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from src.quant_execution_engine.adapters.market_data import MarketDataClient
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.errors import PriceBandExceeded
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

_HUNDRED = Decimal("100")


class PriceBandCheck:
    """Reject LIMIT orders priced too far from last close (advisory)."""

    def __init__(self, settings: Settings, market_data: MarketDataClient) -> None:
        self._settings = settings
        self._market_data = market_data

    @property
    def enabled(self) -> bool:
        """True only when switched on AND a market-data source is configured."""
        return self._settings.price_band_enabled and self._market_data.configured

    async def check(self, order: NormalizedOrder) -> None:
        """Raise :class:`PriceBandExceeded` when the limit is outside the band.

        Skips entirely when disabled/unconfigured or for unpriced (MARKET-style)
        orders. A failed/empty market-data fetch logs a WARNING and passes.
        """
        if not self.enabled:
            return
        if order.price is None:
            return  # MARKET (and other unpriced) orders have no limit to band
        last_close = await self._market_data.last_close(order.symbol, order.market)
        if last_close is None or last_close <= 0:
            logger.warning(
                "price_band.skipped_no_reference symbol=%s reason=%s",
                order.symbol,
                "no_last_close" if last_close is None else "non_positive_last_close",
            )
            return
        deviation_pct = abs(order.price - last_close) / last_close * _HUNDRED
        if deviation_pct > self._settings.price_band_max_pct:
            raise PriceBandExceeded(
                f"price {order.price} deviates {deviation_pct:.4f}% from last close "
                f"{last_close} (max {self._settings.price_band_max_pct}%)",
                client_order_id=order.client_order_id,
                detail={
                    "symbol": order.symbol,
                    "price": format(order.price, "f"),
                    "last_close": format(last_close, "f"),
                    "max_pct": format(self._settings.price_band_max_pct, "f"),
                },
            )


__all__ = ["PriceBandCheck"]

"""Provider failover router (Phase 5, ADR D20).

Routes ``(symbol, market)`` subscriptions to the active provider — a per-symbol
override pins a symbol to a named provider, otherwise the current global active
provider serves it. When the ACTIVE provider records ≥ ``error_threshold``
consecutive errors inside ``window_seconds`` AND a secondary exists, the router
switches the global active provider, resubscribes every active non-overridden
symbol on the secondary, and logs the structured ``order_book.provider_switch``
event. There is no auto-failback (v1 — a restart restores the primary).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping

from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import OrderBookSource
from src.quant_execution_engine.order_book.providers.base import OrderBookProvider

logger = logging.getLogger(__name__)


class ProviderRouter:
    """Active-provider selection + consecutive-error failover."""

    def __init__(
        self,
        *,
        providers: Mapping[OrderBookSource, OrderBookProvider],
        primary: OrderBookSource,
        symbol_overrides: Mapping[str, OrderBookSource],
        error_threshold: int,
        window_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if primary not in providers:
            raise ValueError(f"primary provider {primary} is not configured")
        self._providers = dict(providers)
        self._primary = primary
        self._active = primary
        self._overrides = dict(symbol_overrides)
        self._error_threshold = error_threshold
        self._window_seconds = window_seconds
        self._now = now
        # Active subscriptions keyed (symbol, market) -> the source serving them.
        self._subscriptions: dict[tuple[str, Market], OrderBookSource] = {}
        # Per-source sliding window of consecutive error timestamps.
        self._error_times: dict[OrderBookSource, list[float]] = {}

    @property
    def active(self) -> OrderBookSource:
        """The current global active provider (for tests / health)."""
        return self._active

    @property
    def providers(self) -> tuple[OrderBookProvider, ...]:
        """The configured providers (for runtime start/stop)."""
        return tuple(self._providers.values())

    def _route_for(self, symbol: str) -> OrderBookSource:
        """The provider serving ``symbol``: override wins, else global active."""
        override = self._overrides.get(symbol)
        if override is not None and override in self._providers:
            return override
        return self._active

    async def subscribe(self, symbol: str, market: Market) -> None:
        """Subscribe ``(symbol, market)`` on its routed provider."""
        source = self._route_for(symbol)
        self._subscriptions[(symbol, market)] = source
        await self._providers[source].subscribe(symbol, market)

    async def unsubscribe(self, symbol: str, market: Market) -> None:
        """Unsubscribe ``(symbol, market)`` from the provider serving it."""
        key = (symbol, market)
        source = self._subscriptions.pop(key, None)
        if source is None:
            return
        await self._providers[source].unsubscribe(symbol, market)

    async def on_error(self, source: OrderBookSource, reason: str) -> None:
        """Record a provider error; switch off the active provider on threshold.

        Errors from a non-active provider are logged but never trigger a switch
        (only the active feed's health gates failover).
        """
        now = self._now()
        window = self._error_times.setdefault(source, [])
        window.append(now)
        cutoff = now - self._window_seconds
        window[:] = [t for t in window if t >= cutoff]
        logger.warning(
            "order_book.provider_error source=%s reason=%s count=%d",
            source.value,
            reason,
            len(window),
        )
        if source is not self._active:
            return
        if len(window) < self._error_threshold:
            return
        await self._failover(error_count=len(window))

    def _secondary(self) -> OrderBookSource | None:
        """Any configured provider other than the current active one."""
        for source in self._providers:
            if source is not self._active:
                return source
        return None

    async def _failover(self, *, error_count: int) -> None:
        """Switch the active provider and migrate non-overridden subscriptions."""
        secondary = self._secondary()
        if secondary is None:
            return
        previous = self._active
        # Non-overridden subscriptions currently on the failing provider move.
        moving = [
            (symbol, market)
            for (symbol, market), src in self._subscriptions.items()
            if src is previous and self._overrides.get(symbol) is None
        ]
        self._active = secondary
        self._error_times[previous] = []
        for symbol, market in moving:
            await self._providers[previous].unsubscribe(symbol, market)
            self._subscriptions[(symbol, market)] = secondary
            await self._providers[secondary].subscribe(symbol, market)
        logger.warning(
            "order_book.provider_switch from=%s to=%s symbols=%d errors=%d",
            previous.value,
            secondary.value,
            len(moving),
            error_count,
        )

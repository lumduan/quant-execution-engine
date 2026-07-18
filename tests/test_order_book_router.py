"""ProviderRouter tests: routing, per-symbol override, error handling.

After the broker-023 removal the order book is single-provider (Liberator), so
failover cannot trigger — there is no secondary to switch to. These cover the
single-provider paths plus the no-secondary error path (threshold breaches log
but never switch). The provider-switch migration stays generic in ProviderRouter
for a future second feed.
"""

from __future__ import annotations

import logging

import pytest
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import OrderBookSource
from src.quant_execution_engine.order_book.providers.base import OrderBookProvider
from src.quant_execution_engine.order_book.router import ProviderRouter


class RecordingProvider(OrderBookProvider):
    """A provider that records subscribe/unsubscribe; never connects."""

    def __init__(self, name: OrderBookSource) -> None:
        super().__init__(on_book=lambda _b: None, on_error=lambda _s, _r: None)
        self.name = name  # type: ignore[misc]
        self.subscribed: list[tuple[str, Market]] = []
        self.unsubscribed: list[tuple[str, Market]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def subscribe(self, symbol: str, market: Market) -> None:
        self.subscribed.append((symbol, market))

    async def unsubscribe(self, symbol: str, market: Market) -> None:
        self.unsubscribed.append((symbol, market))


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _router(
    clock: FakeClock,
    *,
    overrides: dict[str, OrderBookSource] | None = None,
    threshold: int = 3,
    window: float = 30.0,
) -> tuple[ProviderRouter, RecordingProvider]:
    provider = RecordingProvider(OrderBookSource.LIBERATOR)
    router = ProviderRouter(
        providers={OrderBookSource.LIBERATOR: provider},
        primary=OrderBookSource.LIBERATOR,
        symbol_overrides=overrides or {},
        error_threshold=threshold,
        window_seconds=window,
        now=clock,
    )
    return router, provider


async def test_subscribe_routes_to_the_provider() -> None:
    clock = FakeClock()
    router, provider = _router(clock)
    await router.subscribe("AOT", Market.SET)
    assert provider.subscribed == [("AOT", Market.SET)]
    assert router.active is OrderBookSource.LIBERATOR
    assert [p.name for p in router.providers] == [OrderBookSource.LIBERATOR]


async def test_override_to_configured_provider_is_honoured() -> None:
    clock = FakeClock()
    router, provider = _router(clock, overrides={"AOT": OrderBookSource.LIBERATOR})
    await router.subscribe("AOT", Market.SET)
    assert provider.subscribed == [("AOT", Market.SET)]


async def test_unsubscribe_routes_and_clears() -> None:
    clock = FakeClock()
    router, provider = _router(clock)
    await router.subscribe("AOT", Market.SET)
    await router.unsubscribe("AOT", Market.SET)
    assert provider.unsubscribed == [("AOT", Market.SET)]
    # Unsubscribing an unknown key is a no-op.
    await router.unsubscribe("ZZZ", Market.SET)


async def test_errors_on_sole_provider_never_switch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no secondary, threshold breaches log but never fail over."""
    clock = FakeClock()
    router, _provider = _router(clock, threshold=2)
    await router.subscribe("AOT", Market.SET)
    with caplog.at_level(logging.WARNING):
        await router.on_error(OrderBookSource.LIBERATOR, "boom")
        await router.on_error(OrderBookSource.LIBERATOR, "boom")  # reaches failover, no secondary
    assert router.active is OrderBookSource.LIBERATOR  # nothing to switch to
    assert "order_book.provider_switch" not in caplog.text
    assert "order_book.provider_error" in caplog.text


async def test_stale_window_errors_age_out() -> None:
    clock = FakeClock()
    router, _provider = _router(clock, threshold=3, window=10)
    await router.subscribe("AOT", Market.SET)
    await router.on_error(OrderBookSource.LIBERATOR, "boom")
    clock.advance(20)  # the first error ages out of the window
    await router.on_error(OrderBookSource.LIBERATOR, "boom")
    await router.on_error(OrderBookSource.LIBERATOR, "boom")
    assert router.active is OrderBookSource.LIBERATOR  # only 2 within the window


async def test_unknown_primary_raises() -> None:
    with pytest.raises(ValueError, match="primary"):
        ProviderRouter(
            providers={},
            primary=OrderBookSource.LIBERATOR,
            symbol_overrides={},
            error_threshold=3,
            window_seconds=30,
        )

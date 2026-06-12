"""ProviderRouter tests: routing, per-symbol override, consecutive-error failover."""

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
) -> tuple[ProviderRouter, RecordingProvider, RecordingProvider]:
    primary = RecordingProvider(OrderBookSource.SETTRADE)
    secondary = RecordingProvider(OrderBookSource.LIBERATOR)
    router = ProviderRouter(
        providers={
            OrderBookSource.SETTRADE: primary,
            OrderBookSource.LIBERATOR: secondary,
        },
        primary=OrderBookSource.SETTRADE,
        symbol_overrides=overrides or {},
        error_threshold=threshold,
        window_seconds=window,
        now=clock,
    )
    return router, primary, secondary


async def test_subscribe_routes_to_primary() -> None:
    clock = FakeClock()
    router, primary, secondary = _router(clock)
    await router.subscribe("AOT", Market.SET)
    assert primary.subscribed == [("AOT", Market.SET)]
    assert secondary.subscribed == []


async def test_failover_after_threshold_switches_and_resubscribes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    router, primary, secondary = _router(clock, threshold=3, window=30)
    await router.subscribe("AOT", Market.SET)
    await router.subscribe("PTT", Market.SET)
    with caplog.at_level(logging.WARNING):
        await router.on_error(OrderBookSource.SETTRADE, "boom")
        await router.on_error(OrderBookSource.SETTRADE, "boom")
        before_switch = router.active
        assert before_switch is OrderBookSource.SETTRADE  # below threshold
        await router.on_error(OrderBookSource.SETTRADE, "boom")  # 3rd -> switch
    after_switch = router.active
    assert after_switch is OrderBookSource.LIBERATOR
    assert set(secondary.subscribed) == {("AOT", Market.SET), ("PTT", Market.SET)}
    assert set(primary.unsubscribed) == {("AOT", Market.SET), ("PTT", Market.SET)}
    assert "order_book.provider_switch" in caplog.text
    assert "from=settrade" in caplog.text and "to=liberator" in caplog.text


async def test_stale_window_errors_do_not_switch() -> None:
    clock = FakeClock()
    router, _primary, _secondary = _router(clock, threshold=3, window=10)
    await router.subscribe("AOT", Market.SET)
    await router.on_error(OrderBookSource.SETTRADE, "boom")
    clock.advance(20)  # the first error ages out of the window
    await router.on_error(OrderBookSource.SETTRADE, "boom")
    await router.on_error(OrderBookSource.SETTRADE, "boom")
    assert router.active is OrderBookSource.SETTRADE  # only 2 within the window


async def test_non_active_provider_errors_never_switch() -> None:
    clock = FakeClock()
    router, _primary, _secondary = _router(clock, threshold=2)
    await router.subscribe("AOT", Market.SET)
    await router.on_error(OrderBookSource.LIBERATOR, "boom")
    await router.on_error(OrderBookSource.LIBERATOR, "boom")
    assert router.active is OrderBookSource.SETTRADE


async def test_overridden_symbol_never_moves() -> None:
    clock = FakeClock()
    router, primary, secondary = _router(
        clock, overrides={"AOT": OrderBookSource.LIBERATOR}, threshold=2
    )
    await router.subscribe("AOT", Market.SET)  # override -> secondary
    await router.subscribe("PTT", Market.SET)  # normal -> primary
    assert secondary.subscribed == [("AOT", Market.SET)]
    assert primary.subscribed == [("PTT", Market.SET)]
    await router.on_error(OrderBookSource.SETTRADE, "boom")
    await router.on_error(OrderBookSource.SETTRADE, "boom")  # switch
    assert router.active is OrderBookSource.LIBERATOR
    # PTT migrated; AOT (overridden) did not get re-subscribed again.
    assert ("PTT", Market.SET) in secondary.subscribed
    assert secondary.subscribed.count(("AOT", Market.SET)) == 1


async def test_unknown_primary_raises() -> None:
    with pytest.raises(ValueError, match="primary"):
        ProviderRouter(
            providers={OrderBookSource.LIBERATOR: RecordingProvider(OrderBookSource.LIBERATOR)},
            primary=OrderBookSource.SETTRADE,
            symbol_overrides={},
            error_threshold=3,
            window_seconds=30,
        )


async def test_unsubscribe_routes_and_clears() -> None:
    clock = FakeClock()
    router, primary, _secondary = _router(clock)
    await router.subscribe("AOT", Market.SET)
    await router.unsubscribe("AOT", Market.SET)
    assert primary.unsubscribed == [("AOT", Market.SET)]
    # Unsubscribing an unknown key is a no-op.
    await router.unsubscribe("ZZZ", Market.SET)

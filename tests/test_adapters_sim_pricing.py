"""SimFillPricer fill-price chain (D21): book → market-data → None."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.sim_pricing import (
    SimFillPricer,
    close_sim_pricer,
    create_sim_pricer,
    get_sim_pricer,
)
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import (
    OrderBook,
    OrderBookLevel,
    OrderBookSource,
)
from src.quant_execution_engine.order_book.service import OrderBookService

from tests.conftest import make_order, make_settings

_BASE = "http://quant-marketdata-engine:8000"
_OHLCV = f"{_BASE}/ohlcv"


class _FakeRouter:
    """Minimal router stub (the pricer never subscribes — pure cache reads)."""

    async def subscribe(self, symbol: str, market: Market) -> None:  # pragma: no cover
        raise AssertionError("the pricer must never subscribe")

    async def unsubscribe(self, symbol: str, market: Market) -> None:  # pragma: no cover
        raise AssertionError("the pricer must never unsubscribe")


def _service() -> OrderBookService:
    return OrderBookService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        max_symbols=500,
        max_age_seconds=5,
        subscriber_queue_size=256,
    )


def _book(*, bid: str, ask: str, symbol: str = "PTT", market: Market = Market.SET) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        market=market,
        bid_levels=[OrderBookLevel(price=Decimal(bid), volume=10)],
        ask_levels=[OrderBookLevel(price=Decimal(ask), volume=10)],
        sequence=1,
        source=OrderBookSource.SETTRADE,
        received_at=datetime.now(UTC),
    )


# ----------------------------------------------------------------- book hop


async def test_warm_book_buy_uses_best_ask() -> None:
    service = _service()
    service.publish(_book(bid="99.5", ask="100.5"))
    pricer = SimFillPricer(service, None, None)
    # No limit bound (MARKET order) ⇒ the raw best ask.
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    assert await pricer.fill_price(order) == Decimal("100.5")


async def test_warm_book_sell_uses_best_bid() -> None:
    service = _service()
    service.publish(_book(bid="99.5", ask="100.5"))
    pricer = SimFillPricer(service, None, None)
    order = make_order(symbol="PTT", side="SELL", order_type="MARKET", price=None)
    assert await pricer.fill_price(order) == Decimal("99.5")


async def test_buy_limit_bounds_below_ask() -> None:
    service = _service()
    service.publish(_book(bid="99.5", ask="100.5"))
    pricer = SimFillPricer(service, None, None)
    # A BUY limit of 100 never fills through 100.5 ⇒ min(100.5, 100) = 100.
    order = make_order(symbol="PTT", side="BUY", order_type="LIMIT", price="100")
    assert await pricer.fill_price(order) == Decimal("100")
    # A BUY limit far above the ask leaves the ask untouched.
    high = make_order(symbol="PTT", side="BUY", order_type="LIMIT", price="200")
    assert await pricer.fill_price(high) == Decimal("100.5")


async def test_sell_limit_bounds_above_bid() -> None:
    service = _service()
    service.publish(_book(bid="99.5", ask="100.5"))
    pricer = SimFillPricer(service, None, None)
    # A SELL limit of 100 never fills through 99.5 ⇒ max(99.5, 100) = 100.
    order = make_order(symbol="PTT", side="SELL", order_type="LIMIT", price="100")
    assert await pricer.fill_price(order) == Decimal("100")
    low = make_order(symbol="PTT", side="SELL", order_type="LIMIT", price="50")
    assert await pricer.fill_price(low) == Decimal("99.5")


async def test_book_logs_hit(caplog: pytest.LogCaptureFixture) -> None:
    service = _service()
    service.publish(_book(bid="99.5", ask="100.5"))
    pricer = SimFillPricer(service, None, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    with caplog.at_level(logging.DEBUG, logger="src.quant_execution_engine.adapters.sim_pricing"):
        await pricer.fill_price(order)
    assert any("sim_pricing.book_hit" in r.message for r in caplog.records)


async def test_empty_side_falls_through_to_none() -> None:
    service = _service()
    # A book with an empty ask side ⇒ BUY can't price from the book; no base_url.
    book = OrderBook(
        symbol="PTT",
        market=Market.SET,
        bid_levels=[OrderBookLevel(price=Decimal("99"), volume=5)],
        ask_levels=[],
        sequence=1,
        source=OrderBookSource.SETTRADE,
        received_at=datetime.now(UTC),
    )
    service.publish(book)
    pricer = SimFillPricer(service, None, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    assert await pricer.fill_price(order) is None


# ------------------------------------------------------------ market-data hop


@respx.mock
async def test_cold_book_falls_back_to_market_data_last_close() -> None:
    payload = {
        "symbol": "SET:PTT",
        "timeframe": "1d",
        "bars": [
            {"ts": "2026-06-10T00:00:00Z", "close": "33.250000"},
            {"ts": "2026-06-11T00:00:00Z", "close": "34.500000"},
        ],
    }
    route = respx.get(_OHLCV).respond(json=payload)
    pricer = SimFillPricer(None, _BASE, SecretStr("read-key"))
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    # max-ts bar (2026-06-11) close, parsed Decimal-as-string.
    assert await pricer.fill_price(order) == Decimal("34.5")
    request = route.calls.last.request
    assert request.url.params["symbol"] == "SET:PTT"
    assert request.url.params["timeframe"] == "1d"
    assert request.headers["X-API-Key"] == "read-key"
    await pricer.aclose()


@respx.mock
async def test_market_data_close_is_limit_bounded() -> None:
    payload = {"bars": [{"ts": "2026-06-11T00:00:00Z", "close": "40.0"}]}
    respx.get(_OHLCV).respond(json=payload)
    pricer = SimFillPricer(None, _BASE, None)
    # BUY limit 35 vs a 40 close ⇒ min(40, 35) = 35.
    order = make_order(symbol="PTT", side="BUY", order_type="LIMIT", price="35")
    assert await pricer.fill_price(order) == Decimal("35")
    await pricer.aclose()


@respx.mock
async def test_tfex_symbol_is_prefixed_tfex() -> None:
    payload = {"bars": [{"ts": "2026-06-11T00:00:00Z", "close": "850.0"}]}
    route = respx.get(_OHLCV).respond(json=payload)
    pricer = SimFillPricer(None, _BASE, None)
    order = make_order(
        symbol="S50M26",
        market="TFEX",
        side="BUY",
        order_type="MARKET",
        price=None,
        position_effect="OPEN",
    )
    await pricer.fill_price(order)
    assert route.calls.last.request.url.params["symbol"] == "TFEX:S50M26"
    await pricer.aclose()


@respx.mock
async def test_market_data_500_returns_none_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(_OHLCV).respond(status_code=500)
    pricer = SimFillPricer(None, _BASE, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    # The fetch failure now warns on the factored-out shared market-data client.
    with caplog.at_level(logging.WARNING, logger="src.quant_execution_engine.adapters.market_data"):
        assert await pricer.fill_price(order) is None
    assert any("market_data.last_close_failed" in r.message for r in caplog.records)
    await pricer.aclose()


@respx.mock
async def test_market_data_timeout_returns_none() -> None:
    respx.get(_OHLCV).mock(side_effect=httpx.ConnectTimeout("slow"))
    pricer = SimFillPricer(None, _BASE, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    assert await pricer.fill_price(order) is None
    await pricer.aclose()


@respx.mock
async def test_market_data_empty_bars_returns_none() -> None:
    respx.get(_OHLCV).respond(json={"bars": []})
    pricer = SimFillPricer(None, _BASE, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    assert await pricer.fill_price(order) is None
    await pricer.aclose()


@respx.mock
async def test_market_data_malformed_bar_returns_none() -> None:
    # Missing 'close' key surfaces as KeyError ⇒ swallowed to None.
    respx.get(_OHLCV).respond(json={"bars": [{"ts": "2026-06-11T00:00:00Z"}]})
    pricer = SimFillPricer(None, _BASE, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    assert await pricer.fill_price(order) is None
    await pricer.aclose()


async def test_no_base_url_goes_straight_to_none(caplog: pytest.LogCaptureFixture) -> None:
    """A bare-sim pricer (no book, no marketdata) is silent — nothing to miss."""
    pricer = SimFillPricer(None, None, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    with caplog.at_level(logging.INFO, logger="src.quant_execution_engine.adapters.sim_pricing"):
        assert await pricer.fill_price(order) is None
    assert not any("sim_pricing.reference_fallback" in r.message for r in caplog.records)
    await pricer.aclose()


async def test_configured_but_cold_book_logs_reference_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With a book configured but cold (and no marketdata), the final hop logs."""
    service = _service()
    pricer = SimFillPricer(service, None, None)
    order = make_order(symbol="PTT", side="BUY", order_type="MARKET", price=None)
    with caplog.at_level(logging.INFO, logger="src.quant_execution_engine.adapters.sim_pricing"):
        assert await pricer.fill_price(order) is None
    assert any("sim_pricing.reference_fallback" in r.message for r in caplog.records)
    await pricer.aclose()


# --------------------------------------------------------------- singletons


async def test_singleton_create_get_close() -> None:
    settings = make_settings(market_data_base_url=_BASE)
    assert get_sim_pricer() is None
    pricer = create_sim_pricer(settings)
    assert get_sim_pricer() is pricer
    # Idempotent: a second create returns the same instance.
    assert create_sim_pricer(settings) is pricer
    await close_sim_pricer()
    assert get_sim_pricer() is None

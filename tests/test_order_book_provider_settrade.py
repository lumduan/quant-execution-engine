"""Settrade provider tests: parser + threadsafe SDK-callback bridge."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from src.quant_execution_engine.adapters.settrade.runtime import SettradeAppCredentials
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import OrderBook, OrderBookSource
from src.quant_execution_engine.order_book.providers import settrade as settrade_mod
from src.quant_execution_engine.order_book.providers.settrade import (
    SettradeOrderBookProvider,
    parse_settrade_bid_offer,
)

_NOW = datetime(2026, 6, 12, tzinfo=UTC)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "bid_price1": "837.8",
        "bid_volume1": 26,
        "bid_price2": "837.7",
        "bid_volume2": 36,
        "ask_price1": "838",
        "ask_volume1": 24,
        "bid_flag": "CEILING",
        "ask_flag": "NORMAL",
        "seq": 99,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- parser


def test_parser_builds_normalized_book() -> None:
    book = parse_settrade_bid_offer(_payload(), symbol="AOT", market=Market.SET, received_at=_NOW)
    assert book is not None
    assert book.source is OrderBookSource.SETTRADE
    assert book.bid_flag == "CEILING"
    assert book.ask_flag == "NORMAL"
    assert book.sequence == 99
    assert [lvl.price for lvl in book.bid_levels] == [Decimal("837.8"), Decimal("837.7")]
    assert book.best_ask is not None and book.best_ask.price == Decimal("838")


def test_parser_drops_zero_and_missing_prices() -> None:
    payload = _payload(bid_price2="0", ask_price1=None)
    book = parse_settrade_bid_offer(payload, symbol="AOT", market=Market.SET, received_at=_NOW)
    assert book is not None
    assert len(book.bid_levels) == 1  # the zero-priced level 2 dropped
    assert book.ask_levels == []


def test_parser_float_price_via_decimal_str() -> None:
    book = parse_settrade_bid_offer(
        {"bid_price1": 837.8, "bid_volume1": 1, "ask_price1": 838.0, "ask_volume1": 1},
        symbol="AOT",
        market=Market.SET,
        received_at=_NOW,
    )
    assert book is not None
    assert book.best_bid is not None and book.best_bid.price == Decimal("837.8")


def test_parser_missing_sequence_defaults_zero() -> None:
    payload = {"bid_price1": "10", "bid_volume1": 5}
    book = parse_settrade_bid_offer(payload, symbol="AOT", market=Market.SET, received_at=_NOW)
    assert book is not None and book.sequence == 0


def test_parser_returns_none_when_both_sides_empty() -> None:
    book = parse_settrade_bid_offer({}, symbol="AOT", market=Market.SET, received_at=_NOW)
    assert book is None


# ----------------------------------------------------------- fake SDK + bridge


class _FakeSubscription:
    def __init__(self, symbol: str, on_message: Any) -> None:
        self.symbol = symbol
        self.on_message = on_message
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def unsubscribe(self) -> None:
        self.stopped = True


class _FakeRealtime:
    def __init__(self) -> None:
        self.subs: list[_FakeSubscription] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def subscribe_bid_offer(self, symbol: str, on_message: Any) -> _FakeSubscription:
        sub = _FakeSubscription(symbol, on_message)
        self.subs.append(sub)
        return sub


class _FakeInvestor:
    def __init__(self, **_kwargs: Any) -> None:
        self.realtime = _FakeRealtime()

    def RealtimeDataConnection(self) -> _FakeRealtime:  # noqa: N802 - SDK name
        return self.realtime


class _FakeSDK:
    Investor = _FakeInvestor


def _creds() -> SettradeAppCredentials:
    return SettradeAppCredentials(
        app_id=SecretStr("id"), app_secret=SecretStr("c2VjcmV0"), app_code="ALGO_EQ"
    )


@pytest.fixture
def _patch_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settrade_mod, "_import_sdk", lambda: _FakeSDK())


async def test_callback_from_thread_delivers_on_loop(_patch_sdk: None) -> None:
    books: list[OrderBook] = []
    provider = SettradeOrderBookProvider(
        on_book=books.append,
        on_error=lambda _s, _r: None,
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()
    await provider.subscribe("AOT", Market.SET)
    sub = provider._realtimes[Market.SET].subs[0]
    assert sub.started

    main_thread = threading.current_thread().ident
    callback_thread: dict[str, int | None] = {}

    def _fire() -> None:
        callback_thread["id"] = threading.current_thread().ident
        sub.on_message(_payload())

    worker = threading.Thread(target=_fire)
    worker.start()
    worker.join()
    # The callback ran on the worker thread, not the loop thread.
    assert callback_thread["id"] != main_thread
    assert books == []  # nothing delivered synchronously from the SDK thread

    # The loop must not be blocked: an unrelated coroutine proceeds, and the
    # bridged book is delivered when the loop next turns.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(books) == 1
    assert books[0].sequence == 99
    assert books[0].best_bid is not None
    assert books[0].best_bid.price == Decimal("837.8")
    await provider.stop()
    assert sub.stopped


async def test_subscribe_idempotent_and_unsubscribe(_patch_sdk: None) -> None:
    provider = SettradeOrderBookProvider(
        on_book=lambda _b: None,
        on_error=lambda _s, _r: None,
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()
    await provider.subscribe("AOT", Market.SET)
    await provider.subscribe("AOT", Market.SET)  # no second SDK subscription
    assert len(provider._realtimes[Market.SET].subs) == 1
    await provider.unsubscribe("AOT", Market.SET)
    assert provider._realtimes[Market.SET].subs[0].stopped


async def test_unconfigured_market_raises_not_configured(_patch_sdk: None) -> None:
    from src.quant_execution_engine.order_book.errors import ProviderNotConfigured

    provider = SettradeOrderBookProvider(
        on_book=lambda _b: None,
        on_error=lambda _s, _r: None,
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()
    with pytest.raises(ProviderNotConfigured):
        await provider.subscribe("S50H25", Market.TFEX)


async def test_subscribe_sdk_failure_reports_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomSDK:
        class Investor:
            def __init__(self, **_kwargs: Any) -> None: ...

            def RealtimeDataConnection(self) -> Any:  # noqa: N802 - SDK name
                raise RuntimeError("sdk down")

    monkeypatch.setattr(settrade_mod, "_import_sdk", lambda: _BoomSDK())
    errors: list[str] = []
    provider = SettradeOrderBookProvider(
        on_book=lambda _b: None,
        on_error=lambda _s, r: errors.append(r),
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()
    await provider.subscribe("AOT", Market.SET)  # swallowed -> on_error, no raise
    assert errors and "subscribe AOT failed" in errors[0]


async def test_parse_error_reports_on_error(
    _patch_sdk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors: list[str] = []
    provider = SettradeOrderBookProvider(
        on_book=lambda _b: None,
        on_error=lambda _s, r: errors.append(r),
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()

    def _boom(*_a: Any, **_k: Any) -> OrderBook | None:
        raise ValueError("bad payload")

    monkeypatch.setattr(settrade_mod, "parse_settrade_bid_offer", _boom)
    provider._parse_and_emit("AOT", Market.SET, {"bid_price1": "1", "bid_volume1": 1})
    assert errors and "parse AOT failed" in errors[0]


async def test_sdk_calls_run_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Investor login / realtime start / subscribe never run on the loop thread.

    Regression: the SDK's constructor + ``start()`` (and even its import) do
    blocking network I/O; they must ride ``asyncio.to_thread``.
    """
    sdk_threads: list[int | None] = []

    class _ThreadRecordingInvestor(_FakeInvestor):
        def __init__(self, **kwargs: Any) -> None:
            sdk_threads.append(threading.current_thread().ident)
            super().__init__(**kwargs)

    class _ThreadRecordingSDK:
        Investor = _ThreadRecordingInvestor

    monkeypatch.setattr(settrade_mod, "_import_sdk", lambda: _ThreadRecordingSDK())
    provider = SettradeOrderBookProvider(
        on_book=lambda _b: None,
        on_error=lambda _s, _r: None,
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()
    await provider.subscribe("AOT", Market.SET)
    loop_thread = threading.current_thread().ident
    assert sdk_threads and all(t != loop_thread for t in sdk_threads)
    await provider.stop()


# --------------------------------------------------- live SDK wire shape (v3)


def test_parser_money_dict_prices_exact_decimal() -> None:
    """BidOfferV3 prices arrive as google.type.Money dicts (units + nanos).

    Regression from the live venue run (2026-06-12): Decimal must be exact —
    units=61 nanos=250000000 is 61.25, never 61.249999….
    """
    payload = {
        "symbol": "AOT",
        "bid_flag": "NORMAL",
        "ask_flag": "NORMAL",
        "bid_price1": {"currency_code": "THB", "units": "61", "nanos": 250000000},
        "bid_volume1": "1500",
        "ask_price1": {"currency_code": "THB", "units": 61, "nanos": 500000000},
        "ask_volume1": 900,
        "bid_price2": {"currency_code": "THB", "units": "0", "nanos": 0},  # ATO zero -> drop
        "bid_volume2": "10",
    }
    book = parse_settrade_bid_offer(payload, symbol="AOT", market=Market.SET, received_at=_NOW)
    assert book is not None
    assert book.bid_levels[0].price == Decimal("61.25")
    assert str(book.bid_levels[0].price) == "61.25"  # 9-dp Money tail stripped
    assert book.bid_levels[0].volume == 1500
    assert book.ask_levels[0].price == Decimal("61.5")
    assert len(book.bid_levels) == 1  # the zero-Money level was dropped


def test_parser_money_integral_price_not_scientific() -> None:
    """An integral Money price (e.g. S50M26 at 775) renders 775, not 7.75E+2."""
    payload = {
        "bid_price1": {"units": "775", "nanos": 0},
        "bid_volume1": 3,
        "ask_price1": {"units": "775", "nanos": 100000000},
        "ask_volume1": 2,
    }
    book = parse_settrade_bid_offer(payload, symbol="S50M26", market=Market.TFEX, received_at=_NOW)
    assert book is not None
    assert str(book.bid_levels[0].price) == "775"
    assert book.ask_levels[0].price == Decimal("775.1")


async def test_deliver_unwraps_sdk_envelope(_patch_sdk: None) -> None:
    """The live SDK wraps messages as {"is_success": True, "data": {...}}."""
    books: list[OrderBook] = []
    provider = SettradeOrderBookProvider(
        on_book=books.append,
        on_error=lambda _s, _r: None,
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()
    provider._parse_and_emit(
        "AOT",
        Market.SET,
        {"is_success": True, "data": _payload()},
    )
    assert len(books) == 1 and books[0].symbol == "AOT"
    await provider.stop()


async def test_deliver_envelope_rejection_reports_on_error(
    _patch_sdk: None, caplog: pytest.LogCaptureFixture
) -> None:
    """is_success=False (e.g. rejectSubscriptions) feeds failover, emits nothing."""
    import logging

    books: list[OrderBook] = []
    errors: list[str] = []
    provider = SettradeOrderBookProvider(
        on_book=books.append,
        on_error=lambda _s, r: errors.append(r),
        market_credentials={Market.SET: _creds()},
        broker_id="023",
    )
    await provider.start()
    with caplog.at_level(logging.WARNING):
        provider._parse_and_emit(
            "AOT", Market.SET, {"is_success": False, "message": "rejectSubscriptions"}
        )
    assert books == []
    assert errors and "rejectSubscriptions" in errors[0]
    assert "order_book.settrade_push_rejected" in caplog.text
    await provider.stop()

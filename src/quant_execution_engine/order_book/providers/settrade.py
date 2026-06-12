"""Settrade realtime bid/offer provider (Phase 5, ADR D18).

The bid/offer feed rides the ``settrade-v2`` SDK's MQTT-backed
``subscribe_bid_offer(symbol, on_message)``. The SDK is imported **lazily** via
the module-level :func:`_import_sdk` seam (tests monkeypatch exactly this one
function); **nothing outside this module imports it**. Importing ``settrade_v2``
has import-time side effects — it writes ``~/settradesdkv2_config.txt``, makes an
NTP call, and performs a version-check HTTP request — so the import only fires
when the order-book service is enabled with a Settrade provider.

The **E21 SDK ban for the ORDER-ROUTING path is unchanged**: order routing uses
the raw-httpx OAuth client, never the sync SDK whose ``requests`` calls would
block the loop. THIS is market-data only — its network loop runs on the SDK's own
thread and delivers SYNCHRONOUS callbacks, which we bridge onto the event loop
with ``loop.call_soon_threadsafe(...)``. We never touch asyncio objects from the
SDK thread and never block the SDK thread on the loop.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, ClassVar

from src.quant_execution_engine.adapters.settrade.runtime import SettradeAppCredentials
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.errors import ProviderError, ProviderNotConfigured
from src.quant_execution_engine.order_book.models import (
    OrderBook,
    OrderBookLevel,
    OrderBookSource,
)
from src.quant_execution_engine.order_book.providers.base import OnBook, OnError, OrderBookProvider

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_MAX_DEPTH = 10


def _import_sdk() -> Any:
    """Import the ``settrade_v2`` SDK lazily (the single test seam)."""
    return importlib.import_module("settrade_v2")


def _decimal_or_none(raw: object) -> Decimal | None:
    """Parse a str/float/int price to ``Decimal`` (via ``str``); drop ≤ 0/bad."""
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _levels(payload: Mapping[str, Any], price_key: str, volume_key: str) -> list[OrderBookLevel]:
    """Build up to 10 levels from flat ``<key>1..10`` price/volume fields."""
    levels: list[OrderBookLevel] = []
    for i in range(1, _MAX_DEPTH + 1):
        price = _decimal_or_none(payload.get(f"{price_key}{i}"))
        if price is None:
            continue
        raw_volume = payload.get(f"{volume_key}{i}")
        volume = int(raw_volume) if raw_volume is not None else 0
        levels.append(OrderBookLevel(price=price, volume=max(volume, 0)))
    return levels


def parse_settrade_bid_offer(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    market: Market,
    received_at: datetime,
) -> OrderBook | None:
    """Parse a flat Settrade bid/offer dict into a normalized ``OrderBook``.

    Keys: ``bid_price1..10`` / ``bid_volume1..10`` / ``ask_price1..10`` /
    ``ask_volume1..10`` + optional ``bid_flag`` / ``ask_flag`` + an optional
    sequence-ish field. Prices arrive as str/float/int; ≤ 0 or missing levels are
    dropped; partial depth is fine. Returns ``None`` if both sides are empty.
    """
    bids = _levels(payload, "bid_price", "bid_volume")
    asks = _levels(payload, "ask_price", "ask_volume")
    if not bids and not asks:
        return None
    sequence = payload.get("seq", payload.get("sequence", payload.get("vs", 0)))
    try:
        sequence_int = int(sequence)
    except (TypeError, ValueError):
        sequence_int = 0
    return OrderBook(
        symbol=symbol,
        market=market,
        bid_levels=bids,
        ask_levels=asks,
        bid_flag=str(payload.get("bid_flag", "NORMAL")),
        ask_flag=str(payload.get("ask_flag", "NORMAL")),
        sequence=sequence_int,
        source=OrderBookSource.SETTRADE,
        received_at=received_at,
    )


class SettradeOrderBookProvider(OrderBookProvider):
    """One ``settrade-v2`` realtime connection per configured market."""

    name: ClassVar[OrderBookSource] = OrderBookSource.SETTRADE

    def __init__(
        self,
        *,
        on_book: OnBook,
        on_error: OnError,
        market_credentials: Mapping[Market, SettradeAppCredentials],
        broker_id: str,
    ) -> None:
        super().__init__(on_book=on_book, on_error=on_error)
        self._market_credentials = dict(market_credentials)
        self._broker_id = broker_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._investors: dict[Market, Any] = {}
        self._realtimes: dict[Market, Any] = {}
        # (symbol, market) -> the SDK subscription object (for unsubscribe).
        self._subs: dict[tuple[str, Market], Any] = {}

    async def start(self) -> None:
        """Capture the running loop the SDK-thread bridge will post onto."""
        self._loop = asyncio.get_running_loop()

    async def stop(self) -> None:
        """Unsubscribe everything and drop the realtime connections.

        SDK teardown is sync/possibly-blocking — keep it off the loop.
        """
        subs = list(self._subs.values())
        self._subs.clear()
        if subs:

            def _teardown() -> None:
                for sub in subs:
                    _safe_unsubscribe(sub)

            await asyncio.to_thread(_teardown)
        self._realtimes.clear()
        self._investors.clear()

    def _realtime_for(self, market: Market) -> Any:
        """Lazily create the SDK Investor + realtime connection for a market."""
        existing = self._realtimes.get(market)
        if existing is not None:
            return existing
        creds = self._market_credentials.get(market)
        if creds is None:
            raise ProviderNotConfigured(f"settrade market {market.value} has no app credentials")
        sdk = _import_sdk()
        investor = sdk.Investor(
            app_id=creds.app_id.get_secret_value(),
            app_secret=creds.app_secret.get_secret_value(),
            app_code=creds.app_code,
            broker_id=self._broker_id,
        )
        realtime = investor.RealtimeDataConnection()
        realtime.start()
        self._investors[market] = investor
        self._realtimes[market] = realtime
        logger.info("order_book.settrade_connected market=%s", market.value)
        return realtime

    def _subscribe_blocking(self, symbol: str, market: Market) -> Any:
        """Runs IN A WORKER THREAD: SDK import/login/MQTT connect + subscribe.

        The SDK's ``Investor`` login, ``RealtimeDataConnection().start()`` and
        even ``import settrade_v2`` itself (NTP + version-check HTTP) all do
        blocking network I/O — none of it may run on the event loop.
        """
        realtime = self._realtime_for(market)

        def _on_message(payload: Mapping[str, Any], _symbol: str = symbol) -> None:
            self._deliver(_symbol, market, payload)

        sub = realtime.subscribe_bid_offer(symbol, _on_message)
        sub.start()
        return sub

    async def subscribe(self, symbol: str, market: Market) -> None:
        """Register ``subscribe_bid_offer`` for ``(symbol, market)``."""
        key = (symbol, market)
        if key in self._subs:
            return
        try:
            self._subs[key] = await asyncio.to_thread(self._subscribe_blocking, symbol, market)
        except ProviderNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 - any SDK failure is failover food
            logger.warning(
                "order_book.settrade_subscribe_failed symbol=%s market=%s err=%r",
                symbol,
                market.value,
                exc,
            )
            self._on_error(self.name, f"subscribe {symbol} failed: {exc!r}")

    async def unsubscribe(self, symbol: str, market: Market) -> None:
        """Stop the SDK subscription for ``(symbol, market)`` (off the loop)."""
        sub = self._subs.pop((symbol, market), None)
        if sub is not None:
            await asyncio.to_thread(_safe_unsubscribe, sub)

    def _deliver(self, symbol: str, market: Market, payload: Mapping[str, Any]) -> None:
        """SDK-thread entry: bridge the parse onto the event loop. Never blocks."""
        loop = self._loop
        if loop is None:  # pragma: no cover - start() always runs first
            return
        loop.call_soon_threadsafe(self._parse_and_emit, symbol, market, dict(payload))

    def _parse_and_emit(self, symbol: str, market: Market, payload: Mapping[str, Any]) -> None:
        """Runs ON the event loop: parse + emit (or report) safely."""
        try:
            book = parse_settrade_bid_offer(
                payload, symbol=symbol, market=market, received_at=datetime.now(UTC)
            )
        except (ProviderError, ValueError) as exc:
            logger.warning("order_book.settrade_parse_failed symbol=%s err=%r", symbol, exc)
            self._on_error(self.name, f"parse {symbol} failed: {exc!r}")
            return
        if book is not None:
            self._on_book(book)


def _safe_unsubscribe(sub: Any) -> None:
    """Best-effort stop of an SDK subscription object (defensive)."""
    for method in ("unsubscribe", "stop"):
        fn: Callable[[], Any] | None = getattr(sub, method, None)
        if callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                logger.debug("order_book.settrade_unsubscribe_ignored err=%r", exc)
            return

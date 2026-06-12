"""Liberator BidOfferV2 provider over raw ``websockets`` (Phase 5, ADR D19).

HARD constraint: **no ``curl_cffi`` anywhere** — it caused frequent disconnects
in the legacy implementation. We use the modern ``websockets`` asyncio client and
a minimal Engine.IO v4 / Socket.IO client (see ``_engineio.py``).

Flow per connection: fetch a FRESH ws-ticket (``POST /ws-ticket``, ``api-key``
header) — the ``ws_url`` is a CREDENTIAL (never logged; only its host); connect;
read the ``0{…}`` open packet; join the default namespaces then ``BidOfferV2``;
resolve each subscribed symbol to its ``orderBookId`` (``GET /order-book/{symbol}``,
cached); batch-join rooms; then an ``async for`` recv loop answering server pings
and emitting normalized books. Reconnect: exponential backoff with jitter, a fresh
ticket every attempt, re-join all rooms on resume, backoff reset after a healthy
period.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import certifi
import httpx
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.errors import (
    SymbolResolutionError,
    TicketAcquisitionError,
)
from src.quant_execution_engine.order_book.models import (
    OrderBook,
    OrderBookLevel,
    OrderBookSource,
)
from src.quant_execution_engine.order_book.providers import _engineio
from src.quant_execution_engine.order_book.providers.base import OnBook, OnError, OrderBookProvider

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0
_HEALTHY_RESET_SECONDS = 30.0
_RAW_LOG_TRUNCATE = 120

# The venue's WS host serves an INCOMPLETE TLS chain (leaf only — verified
# live 2026-06-12: "unable to verify the first certificate"). Browsers paper
# over this with AIA chasing; Python does not. We complete the chain with the
# bundled PUBLIC GlobalSign intermediate so verification stays ON — disabling
# TLS verification on a trading data feed is not an option.
_BUNDLED_CHAIN_PEM = Path(__file__).with_name("liberator_ca_chain.pem")


def build_ssl_context(extra_ca_pem: str | None = None) -> ssl.SSLContext:
    """certifi roots + the bundled venue intermediate (+ an operator extra).

    ``extra_ca_pem`` (``EXECUTION_ENGINE_ORDER_BOOK_LIBERATOR_EXTRA_CA_PEM``)
    lets an operator drop in a replacement intermediate if the venue rotates
    its chain before a code update ships.
    """
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(_BUNDLED_CHAIN_PEM))
    if extra_ca_pem:
        context.load_verify_locations(cafile=extra_ca_pem)
    return context


# (room, sequence, bid levels, ask levels) — the parsed BidOfferV2 update.
_ParsedBook = tuple[int, int, list[OrderBookLevel], list[OrderBookLevel]]


def _price_volume_levels(prices: object, volumes: object) -> list[OrderBookLevel]:
    """Zip parallel price/volume arrays (string prices) to depth, dropping zeros.

    Tolerates partial arrays (< 10) and length mismatches (zip to the shortest).
    """
    if not isinstance(prices, list) or not isinstance(volumes, list):
        return []
    levels: list[OrderBookLevel] = []
    for raw_price, raw_volume in zip(prices, volumes, strict=False):
        try:
            price = Decimal(str(raw_price))
        except (InvalidOperation, ValueError):
            continue
        if price <= 0:
            continue
        try:
            volume = int(raw_volume)
        except (TypeError, ValueError):
            volume = 0
        levels.append(OrderBookLevel(price=price, volume=max(volume, 0)))
    return levels


def parse_bid_offer_payload(payload: Mapping[str, Any]) -> _ParsedBook | None:
    """Parse a BidOfferV2 ``update`` payload into ``(room, vs, bids, asks)``.

    Keys: ``room`` (int), ``vs`` (int sequence), ``bp``/``bv`` (bid prices/volumes,
    index 0 = best), ``op``/``ov`` (asks); ``bmv``/``omv`` MAY be absent. String
    prices to ``Decimal``; zero-price levels dropped; partial arrays tolerated.
    Returns ``None`` when ``room`` is missing/unparseable.
    """
    room_raw = payload.get("room")
    if room_raw is None:
        return None
    try:
        room = int(room_raw)
    except (TypeError, ValueError):
        return None
    try:
        sequence = int(payload.get("vs", 0))
    except (TypeError, ValueError):
        sequence = 0
    bids = _price_volume_levels(payload.get("bp"), payload.get("bv"))
    asks = _price_volume_levels(payload.get("op"), payload.get("ov"))
    return room, sequence, bids, asks


def parse_bid_offer_frame(raw: str) -> _ParsedBook | None:
    """Parse a raw ``42/BidOfferV2,["update",{…}]`` frame; ``None`` if not one."""
    event = _engineio.decode_event(raw)
    if event is None or event.namespace != _engineio.BID_OFFER_NS:
        return None
    if event.name != "update" or not isinstance(event.arg, Mapping):
        return None
    return parse_bid_offer_payload(event.arg)


class _Subscription:
    """One symbol's subscription state (resolved id is filled lazily)."""

    __slots__ = ("symbol", "market", "order_book_id")

    def __init__(self, symbol: str, market: Market) -> None:
        self.symbol = symbol
        self.market = market
        self.order_book_id: int | None = None


class LiberatorOrderBookProvider(OrderBookProvider):
    """ws-ticket + ``websockets`` Engine.IO client for BidOfferV2."""

    name: ClassVar[OrderBookSource] = OrderBookSource.LIBERATOR

    def __init__(
        self,
        *,
        on_book: OnBook,
        on_error: OnError,
        base_url: str,
        api_key: SecretStr,
        http_client: httpx.AsyncClient | None = None,
        connect_timeout: float = 10.0,
        extra_ca_pem: str | None = None,
    ) -> None:
        super().__init__(on_book=on_book, on_error=on_error)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=connect_timeout)
        self._extra_ca_pem = extra_ca_pem
        self._ssl_context: ssl.SSLContext | None = None  # built lazily, cached
        # symbol -> subscription; room id -> subscription (for incoming frames).
        self._subs: dict[str, _Subscription] = {}
        self._by_room: dict[int, _Subscription] = {}
        self._reader: asyncio.Task[None] | None = None
        self._ws: Any | None = None  # the live connection, None while (re)connecting
        self._stopping = False

    async def start(self) -> None:
        """No eager connect — the WS opens lazily on the first subscribe."""
        self._stopping = False

    async def stop(self) -> None:
        """Cancel the reader task and close the httpx client (idempotent)."""
        self._stopping = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._owns_http:
            await self._http.aclose()

    async def subscribe(self, symbol: str, market: Market) -> None:
        """Track ``symbol``; join its room on the live socket or via the reader.

        With a session already up, the room is resolved + joined immediately
        (a session-start-only join would silently starve mid-session
        subscribers until the next reconnect).
        """
        sub = self._subs.get(symbol)
        if sub is None:
            sub = _Subscription(symbol, market)
            self._subs[symbol] = sub
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._run(), name="liberator-orderbook-reader")
            return
        ws = self._ws
        if ws is None:
            return  # (re)connecting — _join_all_rooms will cover this symbol
        try:
            if sub.order_book_id is None:
                sub.order_book_id = await self._resolve_order_book_id(symbol)
            self._by_room[sub.order_book_id] = sub
            await ws.send(_engineio.join_rooms_packet([sub.order_book_id]))
        except (SymbolResolutionError, WebSocketException, OSError) as exc:
            logger.warning("order_book.liberator_live_join_failed symbol=%s err=%r", symbol, exc)
            self._on_error(self.name, f"live join {symbol} failed: {exc!r}")

    async def unsubscribe(self, symbol: str, market: Market) -> None:
        """Drop ``symbol`` and best-effort leave its room on the live socket."""
        sub = self._subs.pop(symbol, None)
        if sub is not None and sub.order_book_id is not None:
            self._by_room.pop(sub.order_book_id, None)
            ws = self._ws
            if ws is not None:
                with contextlib.suppress(WebSocketException, OSError):
                    await ws.send(_engineio.leave_room_packet(sub.order_book_id))
        if not self._subs and self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None

    # ----------------------------------------------------------- connection

    async def _acquire_ticket(self) -> str:
        """POST /ws-ticket → ws_url (a credential — only the host is logged)."""
        url = f"{self._base_url}/ws-ticket"
        try:
            response = await self._http.post(
                url,
                headers={"api-key": self._api_key.get_secret_value()},
                json={"use_us_url": False, "include_metadata": False},
            )
        except httpx.HTTPError as exc:
            raise TicketAcquisitionError(f"ws-ticket request failed: {exc!r}") from exc
        if response.status_code != httpx.codes.OK:
            raise TicketAcquisitionError(f"ws-ticket HTTP {response.status_code}")
        try:
            ws_url = str(response.json()["ws_url"])
        except (ValueError, KeyError, TypeError) as exc:
            raise TicketAcquisitionError("ws-ticket response missing ws_url") from exc
        logger.info("order_book.liberator_ticket host=%s", urlsplit(ws_url).hostname)
        return ws_url

    async def _resolve_order_book_id(self, symbol: str) -> int:
        """GET /order-book/{symbol} → orderBookId (raises on failure)."""
        url = f"{self._base_url}/order-book/{symbol}"
        try:
            response = await self._http.get(url)
        except httpx.HTTPError as exc:
            raise SymbolResolutionError(f"resolve {symbol} failed: {exc!r}") from exc
        if response.status_code != httpx.codes.OK:
            raise SymbolResolutionError(f"resolve {symbol} HTTP {response.status_code}")
        try:
            order_book_id = int(response.json()["orderBookId"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SymbolResolutionError(f"resolve {symbol} missing orderBookId") from exc
        return order_book_id

    async def _run(self) -> None:
        """Connect/recv with reconnect; backoff resets after a healthy period."""
        attempt = 0
        while not self._stopping and self._subs:
            attempt += 1
            delay = _backoff_delay(attempt)
            if attempt > 1:
                logger.warning(
                    "order_book.liberator_reconnect attempt=%d delay=%.1fs", attempt, delay
                )
                await asyncio.sleep(delay)
            connected_at = asyncio.get_running_loop().time()
            try:
                await self._connect_once()
            except (
                TicketAcquisitionError,
                SymbolResolutionError,
                WebSocketException,
                OSError,
            ) as exc:
                logger.warning("order_book.liberator_session_error err=%r", exc)
                self._on_error(self.name, f"session error: {exc!r}")
            if asyncio.get_running_loop().time() - connected_at >= _HEALTHY_RESET_SECONDS:
                attempt = 0

    def _ssl_for(self, ws_url: str) -> ssl.SSLContext | None:
        """The chain-completing context for ``wss://``; ``None`` for plain ws.

        (The local-test path uses ``ws://``, where the ``ssl`` argument must
        be absent; for ``wss://`` the default context would fail on the
        venue's incomplete chain — see :func:`build_ssl_context`.)
        """
        if not ws_url.startswith("wss://"):
            return None
        if self._ssl_context is None:
            self._ssl_context = build_ssl_context(self._extra_ca_pem)
        return self._ssl_context

    async def _connect_once(self) -> None:
        """One session: ticket → handshake → join rooms → recv loop."""
        ws_url = await self._acquire_ticket()
        async with connect(ws_url, ssl=self._ssl_for(ws_url)) as ws:
            await self._handshake(ws)
            self._ws = ws
            try:
                await self._join_all_rooms(ws)
                async for raw in ws:
                    if self._stopping:
                        break
                    await self._handle_frame(ws, str(raw))
            finally:
                self._ws = None

    async def _handshake(self, ws: Any) -> None:
        """Read the open packet, connect the default + BidOfferV2 namespaces."""
        await ws.recv()  # the 0{…} Engine.IO open packet
        await ws.send(_engineio.connect_default_packet())
        for namespace in _engineio.DEFAULT_NAMESPACES:
            await ws.send(_engineio.connect_namespace_packet(namespace))
        await ws.send(_engineio.connect_namespace_packet(_engineio.BID_OFFER_NS))

    async def _join_all_rooms(self, ws: Any) -> None:
        """Resolve ids for all subscribed symbols and batch-join their rooms."""
        self._by_room.clear()
        ids: list[int] = []
        for sub in list(self._subs.values()):
            try:
                if sub.order_book_id is None:
                    sub.order_book_id = await self._resolve_order_book_id(sub.symbol)
            except SymbolResolutionError as exc:
                logger.warning(
                    "order_book.liberator_resolve_failed symbol=%s err=%r", sub.symbol, exc
                )
                self._on_error(self.name, f"resolve {sub.symbol} failed: {exc!r}")
                continue
            self._by_room[sub.order_book_id] = sub
            ids.append(sub.order_book_id)
        if ids:
            await ws.send(_engineio.join_rooms_packet(ids))

    async def _handle_frame(self, ws: Any, raw: str) -> None:
        """Route one raw frame: ping→pong, BidOfferV2 update→emit, else ignore.

        The joined default namespaces (StockV2, TickerV2, …) stream their own
        event frames continuously — those are silently ignored; only a
        malformed frame INSIDE the BidOfferV2 namespace earns a warning.
        """
        kind = _engineio.classify(raw)
        if kind is _engineio.FrameKind.PING:
            await ws.send(_engineio.pong_packet())
            return
        if kind is not _engineio.FrameKind.EVENT:
            return
        event = _engineio.decode_event(raw)
        if event is None or event.namespace != _engineio.BID_OFFER_NS:
            return
        if event.name != "update" or not isinstance(event.arg, Mapping):
            return
        parsed = parse_bid_offer_payload(event.arg)
        if parsed is None:
            logger.warning("order_book.liberator_parse_skip raw=%s", raw[:_RAW_LOG_TRUNCATE])
            return
        room, sequence, bids, asks = parsed
        sub = self._by_room.get(room)
        if sub is None or (not bids and not asks):
            return
        book = OrderBook(
            symbol=sub.symbol,
            market=sub.market,
            bid_levels=bids,
            ask_levels=asks,
            sequence=sequence,
            source=OrderBookSource.LIBERATOR,
            received_at=datetime.now(UTC),
        )
        self._on_book(book)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff (base 1s, cap 60s) with ±50% jitter."""
    base = min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_CAP)
    jitter = 0.5 + random.random()  # uniform in [0.5, 1.5)
    return float(base * jitter)

"""Liberator provider tests: parser, ticket acquisition, reconnect + re-join."""

from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.errors import TicketAcquisitionError
from src.quant_execution_engine.order_book.models import OrderBook, OrderBookSource
from src.quant_execution_engine.order_book.providers import _engineio
from src.quant_execution_engine.order_book.providers.liberator import (
    LiberatorOrderBookProvider,
    parse_bid_offer_frame,
    parse_bid_offer_payload,
)
from websockets.asyncio.server import serve

_BASE = "http://liberator-trading-api:8200/api/v1"

_UPDATE_FRAME = (
    '42/BidOfferV2,["update",{"room":16312965,"vs":1765126847480,'
    '"bp":["837.8","837.7","0"],"bv":[26,36,0],"op":["838","838.1"],"ov":[24,17]}]'
)


# --------------------------------------------------------------------- parser


def test_parse_bid_offer_frame_exact_decimals() -> None:
    parsed = parse_bid_offer_frame(_UPDATE_FRAME)
    assert parsed is not None
    room, sequence, bids, asks = parsed
    assert room == 16312965
    assert sequence == 1765126847480
    # The "0" bid level is dropped; bids keep their string-decimal precision.
    assert [lvl.price for lvl in bids] == [Decimal("837.8"), Decimal("837.7")]
    assert [lvl.volume for lvl in bids] == [26, 36]
    # Asks are shorter than bids — no error, zipped independently.
    assert [lvl.price for lvl in asks] == [Decimal("838"), Decimal("838.1")]


def test_parse_payload_tolerates_absent_bmv_omv_and_partial() -> None:
    payload = {"room": 1, "vs": 9, "bp": ["10.5"], "bv": [3], "op": [], "ov": []}
    parsed = parse_bid_offer_payload(payload)
    assert parsed is not None
    room, seq, bids, asks = parsed
    assert room == 1 and seq == 9
    assert bids[0].price == Decimal("10.5")
    assert asks == []


def test_parse_payload_missing_room_is_none() -> None:
    assert parse_bid_offer_payload({"vs": 1, "bp": ["1"], "bv": [1]}) is None


def test_parse_frame_ignores_non_bidoffer() -> None:
    assert parse_bid_offer_frame('42/StockV2,["update",{"room":1}]') is None
    assert parse_bid_offer_frame("2") is None  # a ping, not an event
    assert parse_bid_offer_frame('42/BidOfferV2,["status",{}]') is None  # wrong event name


def test_length_mismatch_zips_to_shortest() -> None:
    payload = {"room": 1, "vs": 1, "bp": ["10", "11", "12"], "bv": [1, 2]}
    parsed = parse_bid_offer_payload(payload)
    assert parsed is not None
    _room, _seq, bids, _asks = parsed
    assert [lvl.price for lvl in bids] == [Decimal("10"), Decimal("11")]


# --------------------------------------------------------- ticket acquisition


@respx.mock
async def test_ticket_acquisition_failure_raises() -> None:
    respx.post(f"{_BASE}/ws-ticket").mock(return_value=httpx.Response(403))
    provider = LiberatorOrderBookProvider(
        on_book=lambda _b: None,
        on_error=lambda _s, _r: None,
        base_url=_BASE,
        api_key=SecretStr("k"),
    )
    with pytest.raises(TicketAcquisitionError):
        await provider._acquire_ticket()
    await provider.stop()


@respx.mock
async def test_ticket_acquisition_returns_ws_url() -> None:
    respx.post(f"{_BASE}/ws-ticket").mock(
        return_value=httpx.Response(200, json={"ws_url": "wss://host.example/socket.io/?x=1"})
    )
    provider = LiberatorOrderBookProvider(
        on_book=lambda _b: None,
        on_error=lambda _s, _r: None,
        base_url=_BASE,
        api_key=SecretStr("k"),
    )
    assert await provider._acquire_ticket() == "wss://host.example/socket.io/?x=1"
    await provider.stop()


# ---------------------------------------------------- reconnect against server


class _FakeLiberatorServer:
    """Minimal EIO/SIO server: opens, accepts joins, sends one update, closes.

    Records every room-join packet so the test can assert re-join after resume,
    and counts connections so the test can assert the client reconnected.
    """

    def __init__(self) -> None:
        self.connections = 0
        self.join_packets: list[str] = []
        self.first_update_sent = asyncio.Event()
        self.second_connection = asyncio.Event()

    async def handler(self, ws: Any) -> None:
        self.connections += 1
        if self.connections == 2:
            self.second_connection.set()
        await ws.send("0{}")  # Engine.IO open packet
        # Drain the namespace-connect + room-join packets the client sends.
        try:
            async with asyncio.timeout(2):
                while True:
                    raw = await ws.recv()
                    if str(raw).startswith("42/BidOfferV2,") and "join" in str(raw):
                        self.join_packets.append(str(raw))
                        break
        except (TimeoutError, Exception):  # noqa: BLE001 - test teardown is best-effort
            pass
        await ws.send(_UPDATE_FRAME)
        self.first_update_sent.set()
        if self.connections == 1:
            await ws.close()  # force a reconnect on the first session only
        else:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await ws.wait_closed()


@respx.mock
async def test_reconnect_fetches_fresh_ticket_and_rejoins() -> None:
    server_state = _FakeLiberatorServer()
    async with serve(server_state.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws_url = f"ws://127.0.0.1:{port}/socket.io/?EIO=4&transport=websocket"
        ticket_route = respx.post(f"{_BASE}/ws-ticket").mock(
            return_value=httpx.Response(200, json={"ws_url": ws_url})
        )
        respx.get(f"{_BASE}/order-book/AOT").mock(
            return_value=httpx.Response(200, json={"n": "AOT", "orderBookId": 16312965})
        )
        books: list[OrderBook] = []
        provider = LiberatorOrderBookProvider(
            on_book=books.append,
            on_error=lambda _s, _r: None,
            base_url=_BASE,
            api_key=SecretStr("k"),
        )
        await provider.start()
        await provider.subscribe("AOT", Market.SET)
        # First session sends an update then closes; the client reconnects.
        await asyncio.wait_for(server_state.second_connection.wait(), timeout=4)
        await asyncio.sleep(0.2)
        await provider.stop()

    assert server_state.connections >= 2  # it reconnected
    assert ticket_route.call_count >= 2  # a FRESH ticket per attempt
    assert len(server_state.join_packets) >= 2  # re-joined rooms on resume
    assert str(16312965) in server_state.join_packets[0]
    assert any(b.source is OrderBookSource.LIBERATOR and b.symbol == "AOT" for b in books)


# ----------------------------------------------------- unit-level provider paths


class _FakeWS:
    """Captures everything sent; serves a scripted recv queue for the open packet."""

    def __init__(self, recv_script: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self._recv = list(recv_script or ["0{}"])

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return self._recv.pop(0)


def _provider(**kwargs: Any) -> LiberatorOrderBookProvider:
    return LiberatorOrderBookProvider(
        on_book=kwargs.pop("on_book", lambda _b: None),
        on_error=kwargs.pop("on_error", lambda _s, _r: None),
        base_url=_BASE,
        api_key=SecretStr("k"),
        **kwargs,
    )


@respx.mock
async def test_resolve_order_book_id_success_and_failure() -> None:
    respx.get(f"{_BASE}/order-book/AOT").mock(
        return_value=httpx.Response(200, json={"orderBookId": 65537})
    )
    respx.get(f"{_BASE}/order-book/NOPE").mock(return_value=httpx.Response(404))
    provider = _provider()
    assert await provider._resolve_order_book_id("AOT") == 65537
    from src.quant_execution_engine.order_book.errors import SymbolResolutionError

    with pytest.raises(SymbolResolutionError):
        await provider._resolve_order_book_id("NOPE")
    await provider.stop()


async def test_subscribe_starts_reader_unsubscribe_stops_it() -> None:
    provider = _provider()
    await provider.start()
    await provider.subscribe("AOT", Market.SET)
    assert provider._reader is not None
    await provider.subscribe("AOT", Market.SET)  # idempotent: no new sub entry
    assert list(provider._subs) == ["AOT"]
    await provider.unsubscribe("AOT", Market.SET)  # last symbol -> reader cancelled
    assert provider._reader is None
    await provider.stop()


@respx.mock
async def test_handshake_and_join_rooms_emit_packets() -> None:
    respx.get(f"{_BASE}/order-book/AOT").mock(
        return_value=httpx.Response(200, json={"orderBookId": 16312965})
    )
    from src.quant_execution_engine.order_book.providers.liberator import _Subscription

    provider = _provider()
    provider._subs = {"AOT": _Subscription("AOT", Market.SET)}
    ws = _FakeWS()
    await provider._handshake(ws)
    assert _engineio.connect_default_packet() in ws.sent
    assert _engineio.connect_namespace_packet("BidOfferV2") in ws.sent
    await provider._join_all_rooms(ws)
    assert any("join" in s and "16312965" in s for s in ws.sent)
    assert provider._by_room[16312965].symbol == "AOT"
    await provider.stop()


@respx.mock
async def test_join_all_rooms_reports_resolution_failure() -> None:
    respx.get(f"{_BASE}/order-book/AOT").mock(return_value=httpx.Response(500))
    errors: list[str] = []
    from src.quant_execution_engine.order_book.providers.liberator import _Subscription

    provider = _provider(on_error=lambda _s, r: errors.append(r))
    provider._subs = {"AOT": _Subscription("AOT", Market.SET)}
    ws = _FakeWS()
    await provider._join_all_rooms(ws)
    assert errors and "resolve AOT" in errors[0]
    assert provider._by_room == {}
    await provider.stop()


async def test_handle_frame_ping_pong_and_emit() -> None:
    from src.quant_execution_engine.order_book.providers.liberator import _Subscription

    books: list[OrderBook] = []
    provider = _provider(on_book=books.append)
    provider._subs = {"AOT": _Subscription("AOT", Market.SET)}
    provider._subs["AOT"].order_book_id = 16312965
    provider._by_room[16312965] = provider._subs["AOT"]
    ws = _FakeWS()
    await provider._handle_frame(ws, "2")  # ping
    assert ws.sent == ["3"]  # pong
    await provider._handle_frame(ws, _UPDATE_FRAME)  # update -> emit
    assert len(books) == 1 and books[0].symbol == "AOT"
    await provider._handle_frame(ws, "40")  # non-event -> ignored
    await provider.stop()


async def test_handle_frame_parse_skip_logs(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    provider = _provider()
    ws = _FakeWS()
    with caplog.at_level(logging.WARNING):
        await provider._handle_frame(ws, '42/BidOfferV2,["update",{"no":"room"}]')
    assert "order_book.liberator_parse_skip" in caplog.text
    await provider.stop()


async def test_run_loop_logs_session_error_and_reconnect(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from src.quant_execution_engine.order_book.providers.liberator import _Subscription

    # Tiny, deterministic backoff so the reconnect sleep is negligible.
    monkeypatch.setattr(
        "src.quant_execution_engine.order_book.providers.liberator._backoff_delay",
        lambda _attempt: 0.0,
    )
    provider = _provider()
    provider._subs = {"AOT": _Subscription("AOT", Market.SET)}
    attempts = {"n": 0}

    async def _boom() -> None:
        attempts["n"] += 1
        if attempts["n"] >= 2:
            provider._stopping = True  # let the loop exit after the 2nd attempt
        raise TicketAcquisitionError("no ticket")

    monkeypatch.setattr(provider, "_connect_once", _boom)
    with caplog.at_level(logging.WARNING):
        await provider._run()
    assert attempts["n"] >= 2
    assert "order_book.liberator_session_error" in caplog.text
    assert "order_book.liberator_reconnect" in caplog.text
    await provider.stop()


def test_backoff_delay_grows_and_caps() -> None:
    from src.quant_execution_engine.order_book.providers.liberator import _backoff_delay

    # Each delay is within [0.5x, 1.5x] of the (capped) exponential base.
    assert 0.5 <= _backoff_delay(1) <= 1.5
    assert 1.0 <= _backoff_delay(2) <= 3.0
    assert _backoff_delay(50) <= 90.0  # capped at 60 * 1.5 jitter


# ------------------------------------------------------------------- handshake


def test_engineio_helpers() -> None:
    assert _engineio.connect_default_packet() == "40"
    assert _engineio.connect_namespace_packet("BidOfferV2") == "40/BidOfferV2,"
    assert _engineio.pong_packet() == "3"
    assert _engineio.join_rooms_packet([1, 2]) == '42/BidOfferV2,["join", "[1,2]"]'
    assert _engineio.leave_room_packet(5) == '42/BidOfferV2,["leave", "[5]"]'
    assert _engineio.classify("2") is _engineio.FrameKind.PING
    assert _engineio.classify("0{}") is _engineio.FrameKind.OPEN
    assert _engineio.classify("42/BidOfferV2,[]") is _engineio.FrameKind.EVENT
    assert _engineio.decode_event("not-an-event") is None
    assert _engineio.decode_event("42/NS,not-json") is None


# ------------------------------------------------------- mid-session lifecycle


@respx.mock
async def test_subscribe_mid_session_joins_live_room() -> None:
    """A symbol subscribed while the session is up joins its room immediately.

    Regression: a session-start-only join silently starved mid-session
    subscribers until the next reconnect.
    """
    respx.get(f"{_BASE}/order-book/PTT").mock(
        return_value=httpx.Response(200, json={"orderBookId": 999})
    )
    provider = _provider()
    provider._reader = asyncio.create_task(asyncio.sleep(10))  # a "running" reader
    live_ws = _FakeWS()
    provider._ws = live_ws
    try:
        await provider.subscribe("PTT", Market.SET)
        assert provider._by_room[999].symbol == "PTT"
        assert any("join" in s and "999" in s for s in live_ws.sent)
    finally:
        provider._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await provider._reader
        provider._reader = None
        provider._ws = None
        await provider.stop()


@respx.mock
async def test_subscribe_mid_session_join_failure_reports_on_error() -> None:
    respx.get(f"{_BASE}/order-book/BAD").mock(return_value=httpx.Response(500))
    errors: list[str] = []
    provider = _provider(on_error=lambda _s, r: errors.append(r))
    provider._reader = asyncio.create_task(asyncio.sleep(10))
    provider._ws = _FakeWS()
    try:
        await provider.subscribe("BAD", Market.SET)
        assert errors and "live join BAD" in errors[0]
    finally:
        provider._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await provider._reader
        provider._reader = None
        provider._ws = None
        await provider.stop()


async def test_unsubscribe_sends_leave_on_live_socket() -> None:
    from src.quant_execution_engine.order_book.providers.liberator import _Subscription

    provider = _provider()
    sub = _Subscription("AOT", Market.SET)
    sub.order_book_id = 16312965
    provider._subs = {"AOT": sub}
    provider._by_room = {16312965: sub}
    live_ws = _FakeWS()
    provider._ws = live_ws
    await provider.unsubscribe("AOT", Market.SET)
    assert any("leave" in s and "16312965" in s for s in live_ws.sent)
    assert provider._by_room == {}
    await provider.stop()


async def test_handle_frame_ignores_other_namespace_chatter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Default-namespace data frames (StockV2, TickerV2, …) are silent no-ops.

    Regression: they hit the parse-skip WARNING path — continuous log spam
    once the default namespaces are joined.
    """
    import logging

    books: list[OrderBook] = []
    provider = _provider(on_book=books.append)
    ws = _FakeWS()
    with caplog.at_level(logging.WARNING):
        await provider._handle_frame(ws, '42/StockV2,["update",{"x":1}]')
        await provider._handle_frame(ws, '42/TickerV2,["tick",[1,2,3]]')
    assert "order_book.liberator_parse_skip" not in caplog.text
    assert books == [] and ws.sent == []
    await provider.stop()


# ----------------------------------------------------------------- TLS chain


def test_build_ssl_context_with_bundled_intermediate(tmp_path: Any) -> None:
    """The bundled PUBLIC GlobalSign intermediate loads on top of certifi.

    Regression: the venue's WS host serves an incomplete chain (leaf only);
    the default context fails CERTIFICATE_VERIFY_FAILED. Verification stays ON.
    """
    from src.quant_execution_engine.order_book.providers.liberator import (
        _BUNDLED_CHAIN_PEM,
        build_ssl_context,
    )

    assert _BUNDLED_CHAIN_PEM.exists()
    ctx = build_ssl_context()
    stats = ctx.cert_store_stats()
    assert stats["x509_ca"] > 0
    # An operator extra PEM loads on top (reuse the bundled file as the extra).
    extra = tmp_path / "extra.pem"
    extra.write_text(_BUNDLED_CHAIN_PEM.read_text())
    ctx2 = build_ssl_context(str(extra))
    assert ctx2.cert_store_stats()["x509_ca"] >= stats["x509_ca"]


def test_ssl_for_wss_only_and_cached() -> None:
    """wss:// gets the chain-completing context (cached); ws:// gets None."""
    provider = _provider()
    assert provider._ssl_for("ws://127.0.0.1:1234/socket.io/") is None
    ctx = provider._ssl_for("wss://anoucementweb4.liberator.co.th/socket.io/")
    assert ctx is not None
    assert provider._ssl_for("wss://anoucementweb4.liberator.co.th/x") is ctx  # cached

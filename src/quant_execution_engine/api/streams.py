"""The streaming read surface (Phase 5): order-book snapshots/SSE + order updates.

Split from ``routes.py`` to keep both modules inside the repo's file-size
target; included by ``create_app`` alongside the core order router. Everything
here is a READ — api-key-gated but public-mode readable (D24): no raw broker
payload, no account number, no credential ever crosses these routes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.quant_execution_engine.api.deps import (
    get_pool_dep,
    get_settings_dep,
    require_api_key,
)
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.events.errors import OrderStreamUnavailable
from src.quant_execution_engine.events.hub import EventHub, get_event_hub
from src.quant_execution_engine.events.models import GapMarker, OrderUpdateEvent
from src.quant_execution_engine.order_book.errors import OrderBookUnavailable
from src.quant_execution_engine.order_book.models import OrderBook
from src.quant_execution_engine.order_book.runtime import get_order_book_service
from src.quant_execution_engine.order_book.service import OrderBookService

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

# Markets probed, in order, when a snapshot request omits ``market`` (D24).
_PROBE_MARKETS: tuple[Market, ...] = (Market.SET, Market.TFEX)

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ------------------------------------------------------------------ order book


def _require_service() -> OrderBookService:
    """The running order-book service, or a typed 404 when it is disabled."""
    service = get_order_book_service()
    if service is None:
        raise OrderBookUnavailable("order book service is disabled")
    return service


def _snapshot(service: OrderBookService, symbol: str, market: Market | None) -> OrderBook:
    """Read a fresh cached book; probe SET then TFEX when market is omitted."""
    markets = (market,) if market is not None else _PROBE_MARKETS
    for candidate in markets:
        book = service.get(symbol, candidate)
        if book is not None:
            return book
    raise OrderBookUnavailable(f"no fresh order book cached for {symbol}")


@router.get(
    "/order-book/{symbol}",
    dependencies=[Depends(require_api_key)],
    summary="Cached L2 snapshot (public-mode readable; 404 cold)",
)
async def order_book_snapshot(symbol: str, market: Market | None = None) -> JSONResponse:
    """Normalized best-of-book read (D24). 404 when disabled or no fresh book."""
    book = _snapshot(_require_service(), symbol, market)
    return JSONResponse(status_code=status.HTTP_200_OK, content=book.wire_dump())


def _sse_frame(book: OrderBook) -> str:
    """One ``data:`` SSE frame carrying the normalized book (Decimal strings)."""
    return f"data: {json.dumps(book.wire_dump())}\n\n"


async def _order_book_events(
    service: OrderBookService, symbol: str, market: Market, keepalive_seconds: float
) -> AsyncGenerator[str, None]:
    """Snapshot-then-updates SSE generator; keep-alive comments while idle.

    A client disconnect surfaces as cancellation/GeneratorExit — the
    ``async with`` releases the refcounted subscription (no disconnect polling).
    """
    async with service.subscription(symbol, market) as queue:
        snapshot = service.get(symbol, market)
        if snapshot is not None:
            yield _sse_frame(snapshot)
        while True:
            try:
                book = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
            except TimeoutError:
                yield ": keep-alive\n\n"
            else:
                yield _sse_frame(book)


@router.get(
    "/order-book/{symbol}/stream",
    dependencies=[Depends(require_api_key)],
    summary="SSE of normalized book updates (snapshot-then-updates)",
)
async def order_book_stream(
    symbol: str, market: Market, settings: SettingsDep
) -> StreamingResponse:
    """``text/event-stream`` of book updates for ``(symbol, market)`` (D24)."""
    service = _require_service()
    return StreamingResponse(
        _order_book_events(service, symbol, market, settings.stream_keepalive_seconds),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ----------------------------------------------------------- order-update stream


def _require_hub() -> EventHub:
    """The running event hub, or a typed 503 when the lifespan has not run."""
    hub = get_event_hub()
    if hub is None:
        raise OrderStreamUnavailable("order-update stream is unavailable")
    return hub


def _parse_cursor(request: Request, last_event_id: int | None) -> int:
    """Resolve the reconnect cursor: ``Last-Event-ID`` header wins, query falls back.

    An unparseable value is a 422 (not a silent reset) — a client that sends a
    cursor at all is asking for replay and a typo must be visible.
    """
    raw = request.headers.get("Last-Event-ID")
    if raw is None:
        return last_event_id or 0
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid Last-Event-ID",
        ) from exc


def _event_frame(event: OrderUpdateEvent) -> str:
    """One SSE frame: ``id:`` = seq, ``event:`` = the engine-state string."""
    return (
        f"id: {event.seq}\n"
        f"event: {event.engine_state.value}\n"
        f"data: {json.dumps(event.wire_dump())}\n\n"
    )


def _matches(
    event: OrderUpdateEvent,
    *,
    strategy_id: str | None,
    client_order_id: str | None,
    seeded: set[str],
) -> bool:
    """Conjunctive filter; strategy match also includes the DB-seeded cid set.

    A strategy_id match (direct echo OR seeded cid) additively seeds the event's
    cid so later anonymous events for the same order (e.g. an ``ack`` published
    without strategy attribution after a restart) still match.
    """
    if client_order_id is not None and event.client_order_id != client_order_id:
        return False
    if strategy_id is not None:
        if event.strategy_id == strategy_id or event.client_order_id in seeded:
            seeded.add(event.client_order_id)
            return True
        return False
    return True


async def _order_stream_events(
    hub: EventHub,
    *,
    after_seq: int,
    strategy_id: str | None,
    client_order_id: str | None,
    seeded: set[str],
    keepalive_seconds: float,
) -> AsyncGenerator[str, None]:
    """Subscribe-then-replay SSE generator so no event between the two is lost.

    1. Subscribe (live tap) FIRST.
    2. Replay the ring after the cursor (a fallen-off cursor yields one
       ``resync_required`` advisory); track the max replayed seq.
    3. Loop: keep-alive on idle, surface ``gap`` markers, dedupe the
       subscribe/replay overlap by skipping events at/below the max replayed seq,
       apply the filter, and emit the frame.
    """
    async with hub.subscribe() as subscription:
        replay, gap = hub.replay(after_seq)
        if gap:
            yield f'event: resync_required\ndata: {{"after_seq": {after_seq}}}\n\n'
        max_replayed = 0
        for event in replay:
            max_replayed = max(max_replayed, event.seq)
            if _matches(
                event,
                strategy_id=strategy_id,
                client_order_id=client_order_id,
                seeded=seeded,
            ):
                yield _event_frame(event)
        while True:
            try:
                item = await asyncio.wait_for(subscription.queue.get(), timeout=keepalive_seconds)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if isinstance(item, GapMarker):
                yield f'event: gap\ndata: {{"dropped": {subscription.take_dropped()}}}\n\n'
                continue
            if item.seq <= max_replayed:
                continue  # already replayed — dedupe the subscribe/replay overlap
            if _matches(
                item,
                strategy_id=strategy_id,
                client_order_id=client_order_id,
                seeded=seeded,
            ):
                yield _event_frame(item)


@router.get(
    "/orders/stream",
    dependencies=[Depends(require_api_key)],
    summary="SSE of order-update events (filterable; Last-Event-ID reconnect)",
)
async def order_stream(
    request: Request,
    settings: SettingsDep,
    pool: Annotated[asyncpg.Pool, Depends(get_pool_dep)],
    strategy_id: str | None = None,
    client_order_id: str | None = None,
    last_event_id: int | None = None,
) -> StreamingResponse:
    """``text/event-stream`` of normalized order-update events (D14/D15).

    A read (NOT owner-gated): events carry no raw broker payload, no account
    number, no credential. Filters compose conjunctively. ``strategy_id`` seeds
    its historical cids from the durable store at subscribe time so events for
    orders submitted before a restart still match (D16). Reconnect via the
    standard ``Last-Event-ID`` header (or the ``last_event_id`` query fallback);
    a cursor that has fallen off the ring yields one ``resync_required`` frame.
    """
    hub = _require_hub()
    after_seq = _parse_cursor(request, last_event_id)
    seeded: set[str] = set()
    if strategy_id is not None:
        # Load once at stream start; a degraded pool propagates as today's routes do.
        seeded = await repositories.fetch_client_order_ids_for_strategy(pool, strategy_id)
    return StreamingResponse(
        _order_stream_events(
            hub,
            after_seq=after_seq,
            strategy_id=strategy_id,
            client_order_id=client_order_id,
            seeded=seeded,
            keepalive_seconds=settings.stream_keepalive_seconds,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

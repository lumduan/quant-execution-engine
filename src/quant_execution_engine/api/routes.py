"""The order surface (frozen-minimal per the ROADMAP).

Public mode answers only health, capabilities, and reads (E3); order
submission, cancel, amend, and the kill-switch admin are owner-mode. The amend
HTTP route (``PATCH /orders/{cid}``) lands in Phase 4 — the promise the Phase-3
``router.amend`` docstring made is now kept. The ``/admin/*`` routes are
engine-direct only — the gateway never proxies them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.quant_execution_engine import __version__
from src.quant_execution_engine.adapters.liberator.runtime import get_liberator_adapter
from src.quant_execution_engine.adapters.settrade.runtime import get_settrade_adapter
from src.quant_execution_engine.api.deps import (
    get_router_dep,
    get_settings_dep,
    require_api_key,
    require_owner_mode,
)
from src.quant_execution_engine.api.schemas import (
    AmendOrderRequest,
    BrokerRuntimeHealth,
    CapabilitiesResponse,
    HealthResponse,
    KillSwitchEngageResponse,
    KillSwitchStateResponse,
    OrderBookHealth,
)
from src.quant_execution_engine.cache.errors import CacheError
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.capabilities import CAPABILITY_MATRIX
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.order_book.errors import OrderBookUnavailable
from src.quant_execution_engine.order_book.models import OrderBook
from src.quant_execution_engine.order_book.runtime import (
    get_order_book_router,
    get_order_book_service,
)
from src.quant_execution_engine.order_book.service import OrderBookService

router = APIRouter()

RouterDep = Annotated[OrderRouter, Depends(get_router_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

# Markets probed, in order, when a snapshot request omits ``market`` (D24).
_PROBE_MARKETS: tuple[Market, ...] = (Market.SET, Market.TFEX)


def _broker_runtime_health() -> dict[str, BrokerRuntimeHealth] | None:
    """Breaker/session state per configured broker (None when broker-free).

    The dict is non-None when EITHER broker runtime exists; each configured
    broker contributes its own ``{breaker_state, session_healthy}`` entry.
    """
    brokers: dict[str, BrokerRuntimeHealth] = {}
    liberator = get_liberator_adapter()
    if liberator is not None:
        brokers["liberator"] = BrokerRuntimeHealth(
            breaker_state=liberator.breaker.state.value,
            session_healthy=liberator.last_heartbeat_ok,
        )
    settrade = get_settrade_adapter()
    if settrade is not None:
        brokers["settrade"] = BrokerRuntimeHealth(
            breaker_state=settrade.breaker.state.value,
            session_healthy=settrade.last_heartbeat_ok,
            sessions={m.value: ok for m, ok in settrade.last_heartbeat_by_market.items()},
        )
    return brokers or None


def _order_book_health() -> OrderBookHealth | None:
    """Order-book runtime state for /health (None when the service is off)."""
    service = get_order_book_service()
    ob_router = get_order_book_router()
    if service is None or ob_router is None:
        return None
    return OrderBookHealth(
        active_provider=ob_router.active.value,
        providers=[provider.name.value for provider in ob_router.providers],
        cached_symbols=service.cached_symbol_count,
        subscribers=service.subscriber_count,
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    """Mapped to host ``:8400`` (container ``:8000``) in compose."""
    return HealthResponse(
        version=__version__,
        stage=settings.stage,
        public_mode=settings.public_mode,
        brokers=_broker_runtime_health(),
        order_book=_order_book_health(),
    )


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    dependencies=[Depends(require_api_key)],
    summary="Declared per-(broker, market) capability sets",
)
async def capabilities(settings: SettingsDep) -> CapabilitiesResponse:
    """The full static matrix (D7): the router enforces exactly these rows."""
    return CapabilitiesResponse(
        stage=settings.stage,
        capabilities=CAPABILITY_MATRIX,
        brokers=_broker_runtime_health(),
    )


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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/orders",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    status_code=status.HTTP_201_CREATED,
    summary="Submit a NormalizedOrder (idempotent on client_order_id)",
)
async def submit_order(order: NormalizedOrder, order_router: RouterDep) -> JSONResponse:
    """201 on first accept; 200 with the prior result on an idempotent resend."""
    outcome = await order_router.submit(order)
    return JSONResponse(
        status_code=status.HTTP_200_OK if outcome.duplicate else status.HTTP_201_CREATED,
        content=outcome.result.wire_dump(),
    )


@router.get(
    "/orders/{client_order_id}",
    dependencies=[Depends(require_api_key)],
    summary="Read one order's normalized state",
)
async def get_order(client_order_id: str, order_router: RouterDep) -> JSONResponse:
    result = await order_router.get(client_order_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.wire_dump())


@router.delete(
    "/orders/{client_order_id}",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Cancel a resting order (frozen edges only)",
)
async def cancel_order(client_order_id: str, order_router: RouterDep) -> JSONResponse:
    """Deliberately NOT blocked by the kill-switch — cancels reduce risk."""
    result = await order_router.cancel(client_order_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.wire_dump())


@router.patch(
    "/orders/{client_order_id}",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Amend a resting order's price/quantity",
)
async def amend_order(
    client_order_id: str, body: AmendOrderRequest, order_router: RouterDep
) -> JSONResponse:
    """Amend in place (native, same cid) or cancel+replace (returns the new cid).

    The router branches on the order's declared amend semantics: a native broker
    (Settrade) returns the SAME ``client_order_id`` with the updated price/qty;
    a cancel_replace broker (Liberator) returns the REPLACEMENT cid — the honest
    answer, since a new order object was created. The kill-switch is enforced
    INSIDE ``router.amend`` (amends can increase exposure), not duplicated here.
    Typed errors flow through the envelope handlers: 404 order_not_found, 409
    amend_rejected / illegal_transition, 403 public-mode / kill-switch, 422
    risk / capability, 503 broker_circuit_open.
    """
    outcome = await order_router.amend(
        client_order_id,
        new_client_order_id=body.new_client_order_id,
        new_price=body.new_price,
        new_qty=body.new_qty,
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=outcome.result.wire_dump())


@router.get(
    "/admin/kill-switch",
    response_model=KillSwitchStateResponse,
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Kill-switch state",
)
async def kill_switch_state(order_router: RouterDep) -> KillSwitchStateResponse:
    engaged, source = await order_router.kill_switch.status()
    return KillSwitchStateResponse(engaged=engaged, source=source)


@router.post(
    "/admin/kill-switch/engage",
    response_model=KillSwitchEngageResponse,
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Trip the kill-switch: reject all new submits + mass-cancel open orders",
)
async def kill_switch_engage(order_router: RouterDep) -> KillSwitchEngageResponse:
    try:
        await order_router.kill_switch.engage()
    except CacheError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    cancelled, failed = await order_router.mass_cancel()
    return KillSwitchEngageResponse(engaged=True, cancelled=cancelled, failed=failed)


@router.post(
    "/admin/kill-switch/disengage",
    response_model=KillSwitchStateResponse,
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Clear the runtime kill-switch trip (the env flag always wins)",
)
async def kill_switch_disengage(order_router: RouterDep) -> KillSwitchStateResponse:
    try:
        await order_router.kill_switch.disengage()
    except CacheError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    engaged, source = await order_router.kill_switch.status()
    return KillSwitchStateResponse(engaged=engaged, source=source)

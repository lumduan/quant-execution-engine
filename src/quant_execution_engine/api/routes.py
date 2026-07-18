"""The order surface (frozen-minimal per the ROADMAP).

Public mode answers only health, capabilities, and reads (E3); order
submission, cancel, amend, and the kill-switch admin are owner-mode. The amend
HTTP route (``PATCH /orders/{cid}``) lands in Phase 4 — the promise the Phase-3
``router.amend`` docstring made is now kept. The ``/admin/*`` routes are
engine-direct only — the gateway never proxies them. The Phase-5 streaming
read surface (order-book snapshots/SSE + ``GET /orders/stream``) lives in
``api/streams.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from src.quant_execution_engine import __version__
from src.quant_execution_engine.adapters.liberator.runtime import get_liberator_adapter
from src.quant_execution_engine.api.deps import (
    get_operator_id,
    get_router_dep,
    get_settings_dep,
    get_strategy_id,
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
from src.quant_execution_engine.contracts.errors import KillSwitchNotEngagedError
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.order_book.runtime import (
    get_order_book_router,
    get_order_book_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()

RouterDep = Annotated[OrderRouter, Depends(get_router_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _broker_runtime_health() -> dict[str, BrokerRuntimeHealth] | None:
    """Breaker/session state per configured broker (None when broker-free).

    The dict is non-None when a broker runtime exists; each configured broker
    contributes its own ``{breaker_state, session_healthy}`` entry.
    """
    brokers: dict[str, BrokerRuntimeHealth] = {}
    liberator = get_liberator_adapter()
    if liberator is not None:
        brokers["liberator"] = BrokerRuntimeHealth(
            breaker_state=liberator.breaker.state.value,
            session_healthy=liberator.last_heartbeat_ok,
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


@router.post(
    "/orders",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    status_code=status.HTTP_201_CREATED,
    summary="Submit a NormalizedOrder (idempotent on client_order_id)",
)
async def submit_order(
    order: NormalizedOrder,
    order_router: RouterDep,
    strategy_id: Annotated[str | None, Depends(get_strategy_id)],
) -> JSONResponse:
    """201 on first accept; 200 with the prior result on an idempotent resend.

    The optional ``X-Strategy-Id`` header (D16) is stamped onto the order and
    echoed on the order-update stream; absent it, behavior is unchanged.
    """
    outcome = await order_router.submit(order, strategy_id=strategy_id)
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
    (sim) returns the SAME ``client_order_id`` with the updated price/qty; a
    cancel_replace broker (Liberator, Streaming Pro) returns the REPLACEMENT cid
    — the honest answer, since a new order object was created. The kill-switch is
    enforced
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
async def kill_switch_engage(
    order_router: RouterDep,
    operator: Annotated[str, Depends(get_operator_id)],
) -> KillSwitchEngageResponse:
    """Idempotent trip: a second engage returns ``already_engaged=true`` and runs
    NO second mass-cancel. The first engage trips the switch, sweeps all open
    orders, and emits a structured ``kill_switch.engaged`` audit log (operator +
    counts; never any secret).
    """
    engaged, _ = await order_router.kill_switch.status()
    if engaged:
        return KillSwitchEngageResponse(
            engaged=True, already_engaged=True, cancelled_count=0, cancelled=[], failed=[]
        )
    try:
        await order_router.kill_switch.engage()
    except CacheError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    cancelled, failed = await order_router.mass_cancel()
    logger.info(
        "%s",
        json.dumps(
            {
                "event": "kill_switch.engaged",
                "operator": operator,
                "cancelled_count": len(cancelled),
                "failed_count": len(failed),
            }
        ),
    )
    return KillSwitchEngageResponse(
        engaged=True,
        already_engaged=False,
        cancelled_count=len(cancelled),
        cancelled=cancelled,
        failed=failed,
    )


@router.post(
    "/admin/kill-switch/disengage",
    response_model=KillSwitchStateResponse,
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Clear the runtime kill-switch trip (the env flag always wins)",
)
async def kill_switch_disengage(
    order_router: RouterDep,
    operator: Annotated[str, Depends(get_operator_id)],
) -> KillSwitchStateResponse:
    """Disengage the runtime trip. 409 ``kill_switch_not_engaged`` when the switch
    is already clear (distinct from the env-pinned 409); env-pinned disengage
    still raises ``kill_switch_env_pinned``. Emits a structured
    ``kill_switch.disengaged`` audit log with the operator identity.
    """
    engaged, _ = await order_router.kill_switch.status()
    if not engaged:
        # Status-first: a clear switch is a 409 here. This also means the
        # redis-unavailable case is already a 409 (status() with no redis reports
        # not-engaged), so disengage() — which only raises CacheError when redis
        # is absent — has no reachable CacheError path from this route.
        raise KillSwitchNotEngagedError("kill switch is not currently engaged")
    # Env-pinned disengage still raises KillSwitchPinnedError (409 env_pinned).
    await order_router.kill_switch.disengage()
    logger.info("%s", json.dumps({"event": "kill_switch.disengaged", "operator": operator}))
    engaged, source = await order_router.kill_switch.status()
    return KillSwitchStateResponse(engaged=engaged, source=source)

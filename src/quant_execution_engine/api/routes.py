"""The Phase 2 order surface (frozen-minimal per the ROADMAP).

Public mode answers only health, capabilities, and reads (E3); order
submission, cancel, and the kill-switch admin are owner-mode. The amend HTTP
route is deliberately absent until Phase 4 (the adapter method exists). The
``/admin/*`` routes are engine-direct only — the gateway never proxies them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from src.quant_execution_engine import __version__
from src.quant_execution_engine.adapters.liberator.runtime import get_liberator_adapter
from src.quant_execution_engine.api.deps import (
    get_router_dep,
    get_settings_dep,
    require_api_key,
    require_owner_mode,
)
from src.quant_execution_engine.api.schemas import (
    BrokerRuntimeHealth,
    CapabilitiesResponse,
    HealthResponse,
    KillSwitchEngageResponse,
    KillSwitchStateResponse,
)
from src.quant_execution_engine.cache.errors import CacheError
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.capabilities import CAPABILITY_MATRIX
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core.router import OrderRouter

router = APIRouter()

RouterDep = Annotated[OrderRouter, Depends(get_router_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _broker_runtime_health() -> dict[str, BrokerRuntimeHealth] | None:
    """Breaker/session state per configured broker (None when broker-free)."""
    adapter = get_liberator_adapter()
    if adapter is None:
        return None
    return {
        "liberator": BrokerRuntimeHealth(
            breaker_state=adapter.breaker.state.value,
            session_healthy=adapter.last_heartbeat_ok,
        )
    }


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    """Mapped to host ``:8400`` (container ``:8000``) in compose."""
    return HealthResponse(
        version=__version__,
        stage=settings.stage,
        public_mode=settings.public_mode,
        brokers=_broker_runtime_health(),
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

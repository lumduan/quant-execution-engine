"""FastAPI dependencies: settings/pool/redis injection + auth/mode guards."""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, HTTPException, Request, status

from src.quant_execution_engine.adapters.liberator.runtime import get_liberator_adapter
from src.quant_execution_engine.adapters.settrade.runtime import get_settrade_adapter
from src.quant_execution_engine.adapters.sim_pricing import get_sim_pricer
from src.quant_execution_engine.cache.redis_client import get_redis
from src.quant_execution_engine.config.settings import Settings, get_settings
from src.quant_execution_engine.contracts.errors import PublicModeRejected
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.db.postgres import get_pool

logger = logging.getLogger(__name__)


def get_settings_dep() -> Settings:
    return get_settings()


def get_pool_dep() -> asyncpg.Pool:
    return get_pool()


def get_redis_dep() -> Any | None:
    return get_redis()


async def require_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    """hmac-compare ``X-API-Key`` when a key is configured; warn-and-allow otherwise."""
    if settings.api_key is None:
        logger.warning("EXECUTION_ENGINE_API_KEY unset; requests are not authenticated")
        return
    provided = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(provided, settings.api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


async def require_owner_mode(
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    """Order-submission/admin endpoints are owner-mode only (E3)."""
    if settings.public_mode:
        raise PublicModeRejected("endpoint disabled in public mode")


def get_router_dep(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    pool: Annotated[asyncpg.Pool, Depends(get_pool_dep)],
    redis: Annotated[Any | None, Depends(get_redis_dep)],
) -> OrderRouter:
    # Broker adapters are process singletons (breaker/heartbeat state must
    # survive per-request router construction); None when not configured.
    return OrderRouter(
        settings=settings,
        pool=pool,
        redis=redis,
        liberator_adapter=get_liberator_adapter(),
        settrade_adapter=get_settrade_adapter(),
        sim_price_source=get_sim_pricer(),
    )

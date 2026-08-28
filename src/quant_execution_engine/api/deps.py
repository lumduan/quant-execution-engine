"""FastAPI dependencies: settings/pool/redis injection + auth/mode guards."""

from __future__ import annotations

import hmac
import logging
import re
from typing import Annotated, Any

import asyncpg
from fastapi import Depends, HTTPException, Request, status

from src.quant_execution_engine.adapters.liberator.runtime import (
    get_liberator_adapter,
    get_liberator_handle_resolver,
)
from src.quant_execution_engine.adapters.market_data import get_market_data_client
from src.quant_execution_engine.adapters.sim_pricing import get_sim_pricer
from src.quant_execution_engine.adapters.streaming_pro.runtime import get_streaming_pro_adapter
from src.quant_execution_engine.cache.redis_client import get_redis
from src.quant_execution_engine.config.settings import Settings, get_settings
from src.quant_execution_engine.contracts.errors import PublicModeRejected
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.db.postgres import get_pool

logger = logging.getLogger(__name__)

# Conservative slug charset for the strategy identifier (D16): letters, digits,
# and a small set of separators. Bounds length to keep the header trusted but
# tightly shaped (it is stamped durably into execution.orders.strategy_id).
_STRATEGY_ID_MAX_LEN = 64
_STRATEGY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def get_settings_dep() -> Settings:
    return get_settings()


def get_strategy_id(request: Request) -> str | None:
    """Read + validate the optional ``X-Strategy-Id`` header (D16).

    Absent/blank → ``None`` (anonymous submit, behaves exactly as before). A
    present value is length- and charset-checked; a violation is a 422 (the
    header is transport metadata, not order data, but it must be a clean slug
    before it is persisted).
    """
    raw = request.headers.get("X-Strategy-Id")
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > _STRATEGY_ID_MAX_LEN or _STRATEGY_ID_PATTERN.match(value) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid X-Strategy-Id",
        )
    return value


def get_operator_id(request: Request) -> str:
    """Read the optional ``X-Operator-Id`` header for admin audit logging.

    Always returns a value: the trimmed header, or ``"anonymous"`` when absent or
    blank. Deliberately NEVER raises — operator identity is advisory audit
    context, not an auth gate (auth is ``require_api_key`` + ``require_owner_mode``).
    """
    raw = request.headers.get("X-Operator-Id")
    if raw is None:
        return "anonymous"
    value = raw.strip()
    return value or "anonymous"


def get_pool_dep() -> asyncpg.Pool:
    return get_pool()


def get_redis_dep() -> Any | None:
    return get_redis()


async def require_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    """hmac-compare ``X-API-Key``; **fail CLOSED when no key is configured** ([[TK-0462]]).

    🔴 This used to warn-and-allow on a missing ``EXECUTION_ENGINE_API_KEY``, which meant
    *unconfigured* silently equalled *unauthenticated*. Combined with owner mode, every
    guarded route was open to anything that could reach the container — and that is not
    hypothetical: HOME ran that way for weeks because its compose declares no ``env_file``,
    so ``.env`` never reached the process while the guard still *looked* present in the
    code ([[TK-0408]]).

    A misconfiguration must fail **loudly and immediately**, not serve traffic that looks
    healthy. This now matches the platform's own Liberator bridge, which already answered
    the identical question with 503 — two services, one platform, previously opposite
    answers.

    ⚠️ Deliberately **503, not a startup crash.** Requiring the key at settings-load would
    turn the same misconfiguration into crash-on-boot, which is a *larger* blast radius:
    ``/health`` and ``/capabilities`` stay answerable here, so a node that lost its key is
    diagnosable rather than dark.
    """
    if settings.api_key is None:
        logger.error(
            "EXECUTION_ENGINE_API_KEY is not configured — refusing every guarded request "
            "(fail-closed, TK-0462). Set it in the environment this process actually reads."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key authentication is not configured on the server",
        )
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
        streaming_pro_adapter=get_streaming_pro_adapter(),
        sim_price_source=get_sim_pricer(),
        market_data_client=get_market_data_client(),
        handle_resolver=get_liberator_handle_resolver(),
    )

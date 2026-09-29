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
from src.quant_execution_engine.contracts.errors import PublicModeRejected, ReadKeyForbidden
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
    if hmac.compare_digest(provided, settings.api_key):
        return
    if _is_read_key(provided, settings):
        # The read key reached a route that is NOT on the allowlist — which is every route except
        # the account reads, including every order write and the kill-switch. Refused by DEFAULT:
        # a route added later carries this guard unless someone deliberately opts it into
        # ``require_read_or_full_key``, so forgetting to classify a new route fails SAFE.
        raise ReadKeyForbidden(
            "the read-only key is accepted only on the account-read routes; this route needs "
            "the full key",
            detail={"allowlist": sorted(f"{m} {p}" for m, p in READ_KEY_ROUTES)},
        )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


# 🔑 The ONLY routes the read-only key may call ([[TK-0442]] engine layer, GH #395, operator ruling
# 2026-09-29). Native paths; the gateway alias re-mounts the same route objects, so the same guard
# applies there. This set is PINNED by ``tests/test_api_read_key.py``, which walks every mounted
# route: a route that accepts the read key without being listed here fails CI, and so does a
# listed route that does not accept it.
READ_KEY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/accounts/{account}"),
        ("GET", "/accounts/{account}/positions"),
        ("GET", "/accounts/{account}/open-orders"),
        # GH #398, operator ruling 2026-09-29: the history routes join the allowlist.
        ("GET", "/accounts/{account}/orders"),
        ("GET", "/accounts/{account}/venue-orders"),
    }
)


def _is_read_key(provided: str, settings: Settings) -> bool:
    """True only for a configured read key that DIFFERS from the full key and matches.

    A read key equal to the full key would not be scoped at all — presenting it passes the full
    guard first — so it is never treated as a read key, and the collision is logged loudly. The
    full key's holders are unaffected either way.
    """
    read_key = settings.read_api_key
    if not read_key:
        return False
    if settings.api_key is not None and hmac.compare_digest(read_key, settings.api_key):
        logger.error(
            "EXECUTION_ENGINE_READ_API_KEY equals EXECUTION_ENGINE_API_KEY — it is NOT a scoped "
            "key, so it is ignored as one. Generate a distinct value."
        )
        return False
    return hmac.compare_digest(provided, read_key)


async def require_read_or_full_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> None:
    """Accept the full key OR the read-only key. Used ONLY on :data:`READ_KEY_ROUTES`.

    Fails closed exactly like :func:`require_api_key` when no full key is configured (503): a node
    that lost its full key is misconfigured, and serving reads on the read key alone would make
    that state look healthy.
    """
    if settings.api_key is None:
        logger.error(
            "EXECUTION_ENGINE_API_KEY is not configured — refusing every guarded request "
            "(fail-closed, TK-0462), the read-only routes included."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key authentication is not configured on the server",
        )
    provided = request.headers.get("X-API-Key", "")
    if hmac.compare_digest(provided, settings.api_key) or _is_read_key(provided, settings):
        return
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

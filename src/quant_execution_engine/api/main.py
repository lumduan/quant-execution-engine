"""Execution-engine FastAPI app factory + lifespan.

Startup is resilient (marketdata pattern): a missing Postgres degrades order
endpoints (they 500 on the uninitialized pool) while ``/health`` keeps
answering, so compose healthchecks and the gateway proxy stay observable.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.quant_execution_engine import __version__
from src.quant_execution_engine.adapters.liberator.runtime import (
    close_liberator_runtime,
    create_liberator_runtime,
    start_liberator_workers,
)
from src.quant_execution_engine.adapters.settrade.runtime import (
    close_settrade_runtime,
    create_settrade_runtime,
    start_settrade_workers,
)
from src.quant_execution_engine.api.error_handlers import register_error_handlers
from src.quant_execution_engine.api.routes import router
from src.quant_execution_engine.cache.redis_client import close_redis, create_redis
from src.quant_execution_engine.config.settings import get_settings
from src.quant_execution_engine.db.postgres import close_pool, create_pool
from src.quant_execution_engine.logging_config import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Open/close the DB pool and Redis client around the app's lifetime."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "starting quant-execution-engine (public_mode=%s, stage=%s, kill_switch_env=%s)",
        settings.public_mode,
        settings.stage,
        settings.kill_switch_engaged,
    )
    try:
        await create_pool(
            settings.pg_dsn,
            min_size=settings.pg_pool_min_size,
            max_size=settings.pg_pool_max_size,
        )
    except Exception:  # noqa: BLE001 - degrade, never crash the probe surface
        logger.warning("startup: postgres pool unavailable; order endpoints degraded")
    create_redis(settings.redis_url)
    # Broker runtimes: only at broker stages in owner mode with secrets present
    # (sim/public stays broker-free); workers per the stage matrix.
    create_liberator_runtime(settings)
    await start_liberator_workers(settings)
    create_settrade_runtime(settings)
    await start_settrade_workers(settings)
    try:
        yield
    finally:
        await close_settrade_runtime()
        await close_liberator_runtime()
        await close_redis()
        await close_pool()


def create_app() -> FastAPI:
    """Build the FastAPI app."""
    app = FastAPI(
        title="quant-execution-engine",
        version=__version__,
        summary="Canonical order router + sole broker order-routing-credential owner.",
        lifespan=lifespan,
    )
    app.include_router(router)
    register_error_handlers(app)
    return app


app = create_app()

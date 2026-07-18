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
from src.quant_execution_engine.adapters.market_data import (
    close_market_data_client,
    create_market_data_client,
)
from src.quant_execution_engine.adapters.sim_pricing import close_sim_pricer, create_sim_pricer
from src.quant_execution_engine.adapters.streaming_pro.runtime import (
    close_streaming_pro_runtime,
    create_streaming_pro_runtime,
    start_streaming_pro_workers,
)
from src.quant_execution_engine.api.audit import router as audit_router
from src.quant_execution_engine.api.error_handlers import register_error_handlers
from src.quant_execution_engine.api.routes import router
from src.quant_execution_engine.api.streams import router as streams_router
from src.quant_execution_engine.cache.redis_client import close_redis, create_redis
from src.quant_execution_engine.config.settings import get_settings
from src.quant_execution_engine.db.postgres import close_pool, create_pool
from src.quant_execution_engine.events.hub import create_event_hub, reset_event_hub
from src.quant_execution_engine.logging_config import configure_logging
from src.quant_execution_engine.order_book.runtime import (
    close_order_book_runtime,
    create_order_book_runtime,
    start_order_book_workers,
)

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
    # Event hub FIRST (before the pool / any broker runtime): no durable
    # transition can publish before the hub exists, so no state change is missed
    # (D15). In-process, no async teardown — just reset the singleton on exit.
    create_event_hub(settings)
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
    create_streaming_pro_runtime(settings)
    await start_streaming_pro_workers(settings)
    # Order book service (Phase 5): default-off; a no-op unless an operator opts
    # in with a configured provider. Closed first so feeds stop before brokers.
    create_order_book_runtime(settings)
    await start_order_book_workers(settings)
    # Sim fill-price chain (D21): reads the order-book service above + the
    # market-data engine; built after the order book, closed before it.
    create_sim_pricer(settings)
    # Shared market-data client for the Phase-6 price-band check (A2): a process
    # singleton (the per-request router borrows it, never owns it).
    create_market_data_client(settings)
    try:
        yield
    finally:
        await close_market_data_client()
        await close_sim_pricer()
        await close_order_book_runtime()
        await close_streaming_pro_runtime()
        await close_liberator_runtime()
        await close_redis()
        await close_pool()
        reset_event_hub()


def create_app() -> FastAPI:
    """Build the FastAPI app."""
    app = FastAPI(
        title="quant-execution-engine",
        version=__version__,
        summary="Canonical order router + sole broker order-routing-credential owner.",
        lifespan=lifespan,
    )
    # streams BEFORE the core router: /orders/stream must out-rank the
    # /orders/{client_order_id} path parameter (match order = registration order).
    app.include_router(streams_router)
    # audit (Phase 6 / E1-E2) BEFORE the core router too: its literal
    # /admin/audit/export must out-rank the /admin/orders/{cid}/audit path param,
    # and both stay clear of the core /orders/{cid} surface (the /admin prefix is
    # disjoint). Owner-mode is enforced at the audit router level.
    app.include_router(audit_router)
    app.include_router(router)
    register_error_handlers(app)
    return app


app = create_app()

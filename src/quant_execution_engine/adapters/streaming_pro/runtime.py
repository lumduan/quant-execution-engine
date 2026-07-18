"""Process-level Streaming-Pro runtime: adapter singleton + background workers.

``api/deps.py`` builds an ``OrderRouter`` per request, so the breaker, the httpx client, and the
heartbeat/reconcile workers MUST live here as module-level singletons (the Liberator
pattern). The app lifespan calls ``create_streaming_pro_runtime`` + ``start_streaming_pro_workers``
on startup and ``close_streaming_pro_runtime`` on shutdown.

Start predicate: the runtime exists when the stage is ``paper``/``micro_live``/``live`` AND owner
mode is on AND the bridge api-key is present (the bridge owns USERNAME/PASSWORD/PIN — the engine
needs no PIN). A missing api-key logs a WARNING and leaves streaming_pro routing disabled. The
heartbeat runs whenever the runtime exists; the reconciler runs only at ``micro_live``/``live``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from src.quant_execution_engine.adapters.streaming_pro.adapter import StreamingProAdapter
from src.quant_execution_engine.adapters.streaming_pro.heartbeat import heartbeat_loop
from src.quant_execution_engine.adapters.streaming_pro.reconciler import StreamingProReconciler
from src.quant_execution_engine.adapters.streaming_pro.transport import StreamingProTransport
from src.quant_execution_engine.cache.redis_client import get_redis
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Market, Stage
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.postgres import get_pool

logger = logging.getLogger(__name__)

_BROKER_STAGES = frozenset({Stage.PAPER, Stage.MICRO_LIVE, Stage.LIVE})
_RECONCILE_STAGES = frozenset({Stage.MICRO_LIVE, Stage.LIVE})

_adapter: StreamingProAdapter | None = None
_tasks: list[asyncio.Task[None]] = []
_trip_lock = asyncio.Lock()


async def _resolve_order_from_store(client_order_id: str) -> tuple[str, Market, str, str] | None:
    """Durable cid → (order_no, market, account, symbol) lookup the cancel falls back to."""
    row = await repositories.fetch_order(get_pool(), client_order_id)
    if row is None or row.broker_order_id is None:
        return None
    return (row.broker_order_id, row.market, row.account, row.symbol)


def streaming_pro_enabled(settings: Settings) -> bool:
    """The start predicate (see module docstring) — no PIN check (bridge-owned)."""
    return (
        settings.stage in _BROKER_STAGES
        and not settings.public_mode
        and settings.streaming_pro_api_key is not None
    )


def create_streaming_pro_runtime(settings: Settings) -> StreamingProAdapter | None:
    """Create (or return) the singleton adapter; None when not enabled."""
    global _adapter
    if _adapter is not None:
        return _adapter
    if (
        settings.stage in _BROKER_STAGES
        and not settings.public_mode
        and settings.streaming_pro_api_key is None
    ):
        logger.warning(
            "streaming_pro api-key absent at stage '%s'; streaming_pro routing disabled",
            settings.stage,
        )
        return None
    if not streaming_pro_enabled(settings):
        return None
    assert settings.streaming_pro_api_key is not None
    transport = StreamingProTransport(
        base_url=settings.streaming_pro_base_url,
        api_key=settings.streaming_pro_api_key,
    )
    _adapter = StreamingProAdapter(
        transport=transport,
        breaker_threshold=settings.streaming_pro_circuit_breaker_threshold,
        post_rate_limit=settings.streaming_pro_post_rate_limit,
        resolve_order=_resolve_order_from_store,
    )
    logger.info("streaming_pro runtime created (stage=%s)", settings.stage)
    return _adapter


def get_streaming_pro_adapter() -> StreamingProAdapter | None:
    """The singleton, or None before the lifespan created it / when disabled."""
    return _adapter


async def start_streaming_pro_workers(settings: Settings) -> None:
    """Start heartbeat (always when enabled) + reconciler (micro_live/live)."""
    adapter = _adapter
    if adapter is None:
        return

    async def on_trip() -> None:
        """Breaker tripped: best-effort flatten via the router's mass-cancel."""
        async with _trip_lock:
            router = OrderRouter(
                settings=settings,
                pool=get_pool(),
                redis=get_redis(),
                streaming_pro_adapter=adapter,
            )
            cancelled, failed = await router.mass_cancel()
            logger.warning(
                "streaming_pro breaker trip mass-cancel: %d cancelled, %d failed",
                len(cancelled),
                len(failed),
            )

    _tasks.append(
        asyncio.create_task(
            heartbeat_loop(
                adapter,
                interval_seconds=settings.streaming_pro_heartbeat_interval_seconds,
                on_trip=on_trip,
            ),
            name="streaming_pro-heartbeat",
        )
    )
    if settings.stage in _RECONCILE_STAGES:
        reconciler = StreamingProReconciler(
            adapter,
            interval_seconds=settings.streaming_pro_reconcile_interval_seconds,
            pool_provider=get_pool,
        )
        _tasks.append(asyncio.create_task(reconciler.run(), name="streaming_pro-reconciler"))
    logger.info(
        "streaming_pro workers started (heartbeat=%ds, reconciler=%s)",
        settings.streaming_pro_heartbeat_interval_seconds,
        "on" if settings.stage in _RECONCILE_STAGES else "off",
    )


async def close_streaming_pro_runtime() -> None:
    """Cancel workers, close the transport, clear the singleton (idempotent)."""
    global _adapter
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _tasks.clear()
    if _adapter is not None:
        await _adapter.aclose()
        _adapter = None
        logger.info("streaming_pro runtime closed")

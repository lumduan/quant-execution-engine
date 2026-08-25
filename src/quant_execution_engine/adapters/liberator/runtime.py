"""Process-level Liberator runtime: adapter singleton + background workers.

``api/deps.py`` builds an ``OrderRouter`` per request, so the breaker state,
the httpx client, and the heartbeat/reconcile workers MUST live here as
module-level singletons (the ``db/postgres.py`` / ``cache/redis_client.py``
pattern). The app lifespan calls ``create_liberator_runtime`` +
``start_liberator_workers`` on startup and ``close_liberator_runtime`` first
on shutdown.

Start predicate (decision log, Phase 3): the runtime exists when the stage is
``paper``/``micro_live``/``live`` AND owner mode is on AND both Liberator
secrets are present — missing secrets log a WARNING and leave Liberator
routing disabled (micro_live submits then get ``stage_rejected``). The
heartbeat runs whenever the runtime exists (paper needs the live session);
the reconciler runs only at ``micro_live``/``live`` — at ``paper`` placements
land in sim, and reconciling sim-acked rows against venue truth would corrupt
them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
from src.quant_execution_engine.adapters.liberator.heartbeat import heartbeat_loop
from src.quant_execution_engine.adapters.liberator.reconciler import LiberatorReconciler
from src.quant_execution_engine.adapters.liberator.transport import LiberatorTransport
from src.quant_execution_engine.cache.redis_client import get_redis
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Market, Stage
from src.quant_execution_engine.core.handle_recovery import HandleResolver
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.postgres import get_pool

logger = logging.getLogger(__name__)

_BROKER_STAGES = frozenset({Stage.PAPER, Stage.MICRO_LIVE, Stage.LIVE})
_RECONCILE_STAGES = frozenset({Stage.MICRO_LIVE, Stage.LIVE})

_adapter: LiberatorAdapter | None = None
_reconciler: LiberatorReconciler | None = None
_tasks: list[asyncio.Task[None]] = []
_trip_lock = asyncio.Lock()


async def _resolve_order_from_store(client_order_id: str) -> tuple[str, Market] | None:
    """Durable cid → (orderNo, market) lookup the adapter's cancel falls back to."""
    row = await repositories.fetch_order(get_pool(), client_order_id)
    if row is None or row.broker_order_id is None:
        return None
    return (row.broker_order_id, row.market)


def liberator_enabled(settings: Settings) -> bool:
    """The start predicate (see module docstring)."""
    return (
        settings.stage in _BROKER_STAGES
        and not settings.public_mode
        and settings.liberator_api_key is not None
        and settings.liberator_pin is not None
    )


def create_liberator_runtime(settings: Settings) -> LiberatorAdapter | None:
    """Create (or return) the singleton adapter; None when not enabled."""
    global _adapter
    if _adapter is not None:
        return _adapter
    if (
        settings.stage in _BROKER_STAGES
        and not settings.public_mode
        and (settings.liberator_api_key is None or settings.liberator_pin is None)
    ):
        logger.warning(
            "liberator credentials absent at stage '%s'; liberator routing disabled",
            settings.stage,
        )
        return None
    if not liberator_enabled(settings):
        return None
    assert settings.liberator_api_key is not None and settings.liberator_pin is not None
    transport = LiberatorTransport(
        base_url=settings.liberator_base_url,
        api_key=settings.liberator_api_key,
    )
    _adapter = LiberatorAdapter(
        transport=transport,
        pin=settings.liberator_pin,
        breaker_threshold=settings.liberator_circuit_breaker_threshold,
        # Venue-facing placement cap (D2) — place() only.
        post_rate_limit=settings.liberator_post_rate_limit,
        resolve_order=_resolve_order_from_store,
    )
    logger.info("liberator runtime created (stage=%s)", settings.stage)
    return _adapter


def get_liberator_handle_resolver() -> HandleResolver | None:
    """The TK-0423 handle resolver, or None when no reconciler is running.

    None is returned at ``sim``/``paper`` (no reconciler is started there) — which is
    correct rather than a gap: below ``micro_live`` every placement is intercepted to
    ``SimAdapter``, which always issues its own handle, so this path is unreachable.
    """
    reconciler = _reconciler
    if reconciler is None:
        return None
    return reconciler.resolve_order_now


def get_liberator_adapter() -> LiberatorAdapter | None:
    """The singleton, or None before the lifespan created it / when disabled."""
    return _adapter


async def start_liberator_workers(settings: Settings) -> None:
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
                liberator_adapter=adapter,
            )
            cancelled, failed = await router.mass_cancel()
            logger.warning(
                "liberator breaker trip mass-cancel: %d cancelled, %d failed",
                len(cancelled),
                len(failed),
            )

    _tasks.append(
        asyncio.create_task(
            heartbeat_loop(
                adapter,
                interval_seconds=settings.liberator_heartbeat_interval_seconds,
                on_trip=on_trip,
            ),
            name="liberator-heartbeat",
        )
    )
    if settings.stage in _RECONCILE_STAGES:
        global _reconciler
        _reconciler = LiberatorReconciler(
            adapter,
            interval_seconds=settings.liberator_reconcile_interval_seconds,
            pool_provider=get_pool,
        )
        # Held as a singleton so the submit path can borrow ONE venue read for the
        # TK-0423 post-placement burst — same matcher, same executor, so the burst
        # and the steady loop cannot drift apart.
        _tasks.append(asyncio.create_task(_reconciler.run(), name="liberator-reconciler"))
    logger.info(
        "liberator workers started (heartbeat=%ds, reconciler=%s)",
        settings.liberator_heartbeat_interval_seconds,
        "on" if settings.stage in _RECONCILE_STAGES else "off",
    )


async def close_liberator_runtime() -> None:
    """Cancel workers, close the transport, clear the singleton (idempotent)."""
    global _adapter, _reconciler
    _reconciler = None
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _tasks.clear()
    if _adapter is not None:
        await _adapter.aclose()
        _adapter = None
        logger.info("liberator runtime closed")

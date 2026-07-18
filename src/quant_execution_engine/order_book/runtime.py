"""Process-level order-book runtime: service + router + provider (Phase 5).

Mirrors the broker-adapter runtimes (``adapters/liberator/runtime.py``): the
service, the failover router, the Liberator provider, and their background tasks
live here as module-level singletons because ``api/deps.py`` builds per-request
objects. The app lifespan calls ``create_order_book_runtime`` +
``start_order_book_workers`` on startup and ``close_order_book_runtime`` first on
shutdown.

Start predicate (D24): the runtime exists only when ``order_book_enabled`` is true
AND the Liberator provider is configurable (``liberator_api_key`` present). When
disabled (the default) every function is a no-op and ``get_order_book_service()``
is ``None`` — the engine behaves exactly as before. Liberator is the sole
order-book provider; the failover ``ProviderRouter`` stays provider-generic for a
future second feed.
"""

from __future__ import annotations

import asyncio
import logging

from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.order_book.models import OrderBook, OrderBookSource
from src.quant_execution_engine.order_book.providers.base import OrderBookProvider
from src.quant_execution_engine.order_book.providers.liberator import LiberatorOrderBookProvider
from src.quant_execution_engine.order_book.router import ProviderRouter
from src.quant_execution_engine.order_book.service import OrderBookService

logger = logging.getLogger(__name__)

_service: OrderBookService | None = None
_router: ProviderRouter | None = None
_tasks: list[asyncio.Task[None]] = []


def _liberator_configured(settings: Settings) -> bool:
    return settings.liberator_api_key is not None


def order_book_enabled(settings: Settings) -> bool:
    """The start predicate (see module docstring)."""
    if not settings.order_book_enabled:
        return False
    return _liberator_configured(settings)


def _on_book(book: OrderBook) -> None:
    """Provider → service bridge (runs on the loop)."""
    if _service is not None:
        _service.publish(book)


def _on_error(source: OrderBookSource, reason: str) -> None:
    """Provider → router failover signal (schedule the async handler)."""
    if _router is None:
        return
    task = asyncio.create_task(_router.on_error(source, reason), name="order-book-on-error")
    _tasks.append(task)
    task.add_done_callback(lambda t: _tasks.remove(t) if t in _tasks else None)


def _build_providers(settings: Settings) -> dict[OrderBookSource, OrderBookProvider]:
    """Build the configured provider(s) — Liberator today."""
    providers: dict[OrderBookSource, OrderBookProvider] = {}
    if settings.liberator_api_key is not None:
        providers[OrderBookSource.LIBERATOR] = LiberatorOrderBookProvider(
            on_book=_on_book,
            on_error=_on_error,
            base_url=settings.liberator_base_url,
            api_key=settings.liberator_api_key,
            extra_ca_pem=settings.order_book_liberator_extra_ca_pem,
        )
    return providers


def _parse_overrides(settings: Settings) -> dict[str, OrderBookSource]:
    """Map symbol→provider overrides to known sources (skip unknown names).

    An override naming a valid-but-unconfigured provider is tolerated here and
    guarded downstream by ``ProviderRouter._route_for`` (it falls back to the
    active provider), so no configured-provider check is needed at this layer.
    """
    overrides: dict[str, OrderBookSource] = {}
    for symbol, name in settings.order_book_symbol_overrides.items():
        try:
            source = OrderBookSource(name)
        except ValueError:
            logger.warning(
                "order_book: ignoring override for %s — unknown provider %r", symbol, name
            )
            continue
        overrides[symbol] = source
    return overrides


def create_order_book_runtime(settings: Settings) -> OrderBookService | None:
    """Create (or return) the singleton service; None when not enabled."""
    global _service, _router
    if _service is not None:
        return _service
    if not order_book_enabled(settings):
        if settings.order_book_enabled:
            logger.warning("order_book enabled but no provider configured; service disabled")
        return None
    providers = _build_providers(settings)
    _router = ProviderRouter(
        providers=providers,
        primary=OrderBookSource.LIBERATOR,
        symbol_overrides=_parse_overrides(settings),
        error_threshold=settings.order_book_failover_error_threshold,
        window_seconds=settings.order_book_failover_window_seconds,
    )
    _service = OrderBookService(
        router=_router,
        max_symbols=settings.order_book_cache_max_symbols,
        max_age_seconds=settings.order_book_cache_max_age_seconds,
        subscriber_queue_size=settings.stream_subscriber_queue_size,
    )
    logger.info(
        "order_book runtime created (primary=%s, providers=%s)",
        _router.active.value,
        ",".join(p.value for p in providers),
    )
    return _service


async def start_order_book_workers(settings: Settings) -> None:
    """Start each configured provider (idempotent; no-op when disabled)."""
    if _router is None:
        return
    for provider in _router.providers:
        await provider.start()
    logger.info("order_book workers started")


def get_order_book_service() -> OrderBookService | None:
    """The singleton, or None before the lifespan created it / when disabled."""
    return _service


def get_order_book_router() -> ProviderRouter | None:
    """The failover router singleton (for /health active/providers); None off."""
    return _router


async def close_order_book_runtime() -> None:
    """Stop providers + tasks, clear the singletons (idempotent)."""
    global _service, _router
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    if _router is not None:
        for provider in _router.providers:
            await provider.stop()
    if _service is not None:
        logger.info("order_book runtime closed")
    _service = None
    _router = None

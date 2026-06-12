"""Process-level order-book runtime: service + router + providers (Phase 5).

Mirrors the broker-adapter runtimes (``adapters/liberator/runtime.py``): the
service, the failover router, the providers, and their background tasks live here
as module-level singletons because ``api/deps.py`` builds per-request objects. The
app lifespan calls ``create_order_book_runtime`` + ``start_order_book_workers`` on
startup and ``close_order_book_runtime`` first on shutdown.

Start predicate (D24): the runtime exists only when ``order_book_enabled`` is true
AND at least one provider is configurable — Liberator needs ``liberator_api_key``;
Settrade needs any resolvable per-market app trio (reusing the Phase-4.1
resolution). When disabled (the default) every function is a no-op and
``get_order_book_service()`` is ``None`` — the engine behaves exactly as before.
"""

from __future__ import annotations

import asyncio
import logging

from src.quant_execution_engine.adapters.settrade.runtime import (
    SettradeAppCredentials,
    _configured_markets,
)
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book.models import OrderBook, OrderBookSource
from src.quant_execution_engine.order_book.providers.base import OrderBookProvider
from src.quant_execution_engine.order_book.providers.liberator import LiberatorOrderBookProvider
from src.quant_execution_engine.order_book.providers.settrade import SettradeOrderBookProvider
from src.quant_execution_engine.order_book.router import ProviderRouter
from src.quant_execution_engine.order_book.service import OrderBookService

logger = logging.getLogger(__name__)

_service: OrderBookService | None = None
_router: ProviderRouter | None = None
_tasks: list[asyncio.Task[None]] = []


def _settrade_markets(settings: Settings) -> dict[Market, SettradeAppCredentials]:
    """Per-market Settrade app trios (Phase-4.1 resolution), if any."""
    if settings.settrade_broker_id is None:
        return {}
    return _configured_markets(settings)


def _liberator_configured(settings: Settings) -> bool:
    return settings.liberator_api_key is not None


def _settrade_configured(settings: Settings) -> bool:
    return bool(_settrade_markets(settings))


def order_book_enabled(settings: Settings) -> bool:
    """The start predicate (see module docstring)."""
    if not settings.order_book_enabled:
        return False
    return _liberator_configured(settings) or _settrade_configured(settings)


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
    """Build the configured providers (Settrade and/or Liberator)."""
    providers: dict[OrderBookSource, OrderBookProvider] = {}
    settrade_markets = _settrade_markets(settings)
    if settrade_markets and settings.settrade_broker_id is not None:
        providers[OrderBookSource.SETTRADE] = SettradeOrderBookProvider(
            on_book=_on_book,
            on_error=_on_error,
            market_credentials=settrade_markets,
            broker_id=settings.settrade_broker_id,
        )
    if settings.liberator_api_key is not None:
        providers[OrderBookSource.LIBERATOR] = LiberatorOrderBookProvider(
            on_book=_on_book,
            on_error=_on_error,
            base_url=settings.liberator_base_url,
            api_key=settings.liberator_api_key,
        )
    return providers


def _resolve_primary(
    settings: Settings, providers: dict[OrderBookSource, OrderBookProvider]
) -> OrderBookSource:
    """The configured primary if present, else the only configured provider."""
    primary = OrderBookSource(settings.order_book_primary_provider)
    if primary in providers:
        return primary
    fallback = next(iter(providers))
    logger.warning(
        "order_book: primary provider %s is not configured; using %s",
        primary.value,
        fallback.value,
    )
    return fallback


def _parse_overrides(
    settings: Settings, providers: dict[OrderBookSource, OrderBookProvider]
) -> dict[str, OrderBookSource]:
    """Map symbol→provider overrides to known sources (skip the unknown)."""
    overrides: dict[str, OrderBookSource] = {}
    for symbol, name in settings.order_book_symbol_overrides.items():
        try:
            source = OrderBookSource(name)
        except ValueError:
            logger.warning(
                "order_book: ignoring override for %s — unknown provider %r", symbol, name
            )
            continue
        if source not in providers:
            logger.warning(
                "order_book: ignoring override for %s — provider %s not configured",
                symbol,
                source.value,
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
        primary=_resolve_primary(settings, providers),
        symbol_overrides=_parse_overrides(settings, providers),
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

"""Process-level Settrade runtime: adapter singleton + background workers.

``api/deps.py`` builds an ``OrderRouter`` per request, so the breaker state,
the OAuth client, and the heartbeat/reconcile workers MUST live here as
module-level singletons (the Liberator-runtime pattern). The app lifespan calls
``create_settrade_runtime`` + ``start_settrade_workers`` on startup and
``close_settrade_runtime`` first on shutdown.

Start predicate (Design Decision 11): the runtime exists when the stage is
``paper``/``micro_live``/``live`` AND owner mode is on AND all of
``app_id``/``app_secret``/``app_code``/``broker_id``/``pin`` are present —
``account_no`` is an integration-test convenience, NOT required (the per-order
account rides each ``NormalizedOrder``). Missing secrets log a WARNING and leave
Settrade routing disabled (micro_live submits then get ``stage_rejected``). The
heartbeat runs whenever the runtime exists (paper needs the live session for
reads); the reconciler runs only at ``micro_live``/``live`` — at ``paper``
placements land in sim, and reconciling sim-acked rows against venue truth would
corrupt them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from pydantic import SecretStr

from src.quant_execution_engine.adapters.liberator.runtime import get_liberator_adapter
from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
from src.quant_execution_engine.adapters.settrade.client import SettradeClient
from src.quant_execution_engine.adapters.settrade.heartbeat import heartbeat_loop
from src.quant_execution_engine.adapters.settrade.reconciler import SettradeReconciler
from src.quant_execution_engine.cache.redis_client import get_redis
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Market, Stage
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.postgres import get_pool

logger = logging.getLogger(__name__)

_BROKER_STAGES = frozenset({Stage.PAPER, Stage.MICRO_LIVE, Stage.LIVE})
_RECONCILE_STAGES = frozenset({Stage.MICRO_LIVE, Stage.LIVE})

_adapter: SettradeAdapter | None = None
_tasks: list[asyncio.Task[None]] = []
_trip_lock = asyncio.Lock()


@dataclass(frozen=True)
class SettradeAppCredentials:
    """One market's effective OAuth app trio (Phase 4.1).

    Frozen + hashable (``SecretStr`` is hashable and value-equal), so two markets
    resolving to the SAME trio key one ``SettradeClient`` instance in the dedupe
    map — the sandbox single-app path keeps exactly one login/session.
    """

    app_id: SecretStr
    app_secret: SecretStr
    app_code: str


# (per-market override field names, shared fallback field names) per market — the
# resolution order is documented in settings.py and enforced below.
_MARKET_FIELD_NAMES: dict[Market, tuple[str, str, str]] = {
    Market.SET: (
        "settrade_equity_app_id",
        "settrade_equity_app_secret",
        "settrade_equity_app_code",
    ),
    Market.TFEX: (
        "settrade_derivatives_app_id",
        "settrade_derivatives_app_secret",
        "settrade_derivatives_app_code",
    ),
}
_SHARED_FIELD_NAMES: tuple[str, str, str] = (
    "settrade_app_id",
    "settrade_app_secret",
    "settrade_app_code",
)


def _trio(
    settings: Settings, field_names: tuple[str, str, str]
) -> tuple[SecretStr | None, SecretStr | None, str | None]:
    app_id = getattr(settings, field_names[0])
    app_secret = getattr(settings, field_names[1])
    app_code = getattr(settings, field_names[2])
    return app_id, app_secret, app_code


def _effective_credentials(settings: Settings, market: Market) -> SettradeAppCredentials | None:
    """Resolve one market's effective trio (the core rule — see settings.py).

    Per-market trio complete → use it. Per-market trio PARTIAL → market
    UNCONFIGURED + WARNING naming the missing fields (no silent shared fallback).
    Else shared trio complete → use shared. Else unconfigured.
    """
    override_names = _MARKET_FIELD_NAMES[market]
    app_id, app_secret, app_code = _trio(settings, override_names)
    present = [v is not None for v in (app_id, app_secret, app_code)]
    if all(present):
        assert app_id is not None and app_secret is not None and app_code is not None
        return SettradeAppCredentials(app_id=app_id, app_secret=app_secret, app_code=app_code)
    if any(present):
        missing = [name for name, ok in zip(override_names, present, strict=True) if not ok]
        logger.warning(
            "settrade %s app trio is PARTIAL (missing %s) — market UNCONFIGURED, NO fallback "
            "to the shared app; %s orders will be rejected",
            market.value,
            ", ".join(f"EXECUTION_ENGINE_{n.upper()}" for n in missing),
            market.value,
        )
        return None
    shared_id, shared_secret, shared_code = _trio(settings, _SHARED_FIELD_NAMES)
    if shared_id is not None and shared_secret is not None and shared_code is not None:
        return SettradeAppCredentials(
            app_id=shared_id, app_secret=shared_secret, app_code=shared_code
        )
    return None


def _configured_markets(settings: Settings) -> dict[Market, SettradeAppCredentials]:
    """Markets with a resolvable effective trio, in (SET, TFEX) order."""
    resolved: dict[Market, SettradeAppCredentials] = {}
    for market in (Market.SET, Market.TFEX):
        creds = _effective_credentials(settings, market)
        if creds is not None:
            resolved[market] = creds
    return resolved


def _credentials_source(settings: Settings, market: Market) -> str:
    """'per-market' | 'shared' for the boot log (never a secret value)."""
    app_id, _, _ = _trio(settings, _MARKET_FIELD_NAMES[market])
    return "per-market" if app_id is not None else "shared"


async def _resolve_order_from_store(
    client_order_id: str,
) -> tuple[str, Market, str] | None:
    """Durable cid → (orderNo, market, account) lookup the adapter falls back to."""
    row = await repositories.fetch_order(get_pool(), client_order_id)
    if row is None or row.broker_order_id is None:
        return None
    return (row.broker_order_id, row.market, row.account)


def _secrets_present(settings: Settings) -> bool:
    """``broker_id`` + ``pin`` + at least one market with a resolvable app trio.

    Phase 4.1: the per-market/shared resolution replaces the old fixed shared
    trio (``account_no`` is still NOT required — the per-order account rides each
    ``NormalizedOrder``).
    """
    return (
        settings.settrade_broker_id is not None
        and settings.settrade_pin is not None
        and bool(_configured_markets(settings))
    )


def settrade_enabled(settings: Settings) -> bool:
    """The start predicate (see module docstring)."""
    return (
        settings.stage in _BROKER_STAGES and not settings.public_mode and _secrets_present(settings)
    )


def create_settrade_runtime(settings: Settings) -> SettradeAdapter | None:
    """Create (or return) the singleton adapter; None when not enabled."""
    global _adapter
    if _adapter is not None:
        return _adapter
    if (
        settings.stage in _BROKER_STAGES
        and not settings.public_mode
        and not _secrets_present(settings)
    ):
        logger.warning(
            "settrade credentials absent at stage '%s'; settrade routing disabled",
            settings.stage,
        )
        return None
    if not settrade_enabled(settings):
        return None
    assert settings.settrade_broker_id is not None
    assert settings.settrade_pin is not None
    markets = _configured_markets(settings)
    # Dedupe by credentials value: markets sharing a trio share ONE client (the
    # sandbox single-app path ⇒ one login, one session).
    by_creds: dict[SettradeAppCredentials, SettradeClient] = {}
    clients: dict[Market, SettradeClient] = {}
    for market, creds in markets.items():
        client = by_creds.get(creds)
        if client is None:
            client = SettradeClient(
                base_url=settings.settrade_base_url,
                app_id=creds.app_id,
                app_secret=creds.app_secret,
                app_code=creds.app_code,
                broker_id=settings.settrade_broker_id,
                refresh_margin_seconds=settings.settrade_token_refresh_margin_seconds,
            )
            by_creds[creds] = client
        clients[market] = client
    if len(markets) == 1:
        only = next(iter(markets))
        missing = next(m for m in (Market.SET, Market.TFEX) if m is not only)
        logger.warning(
            "settrade: only the %s market is configured — %s orders will be rejected "
            "(no broker app)",
            only.value,
            missing.value,
        )
    _adapter = SettradeAdapter(
        clients=clients,
        broker_id=settings.settrade_broker_id,
        pin=settings.settrade_pin,
        breaker_threshold=settings.settrade_circuit_breaker_threshold,
        resolve_order=_resolve_order_from_store,
    )
    sources = ",".join(f"{m.value}:{_credentials_source(settings, m)}" for m in markets)
    logger.info(
        "settrade runtime created (stage=%s, markets=%s, clients=%d)",
        settings.stage,
        sources,
        len(by_creds),
    )
    return _adapter


def get_settrade_adapter() -> SettradeAdapter | None:
    """The singleton, or None before the lifespan created it / when disabled."""
    return _adapter


async def start_settrade_workers(settings: Settings) -> None:
    """Start heartbeat (always when enabled) + reconciler (micro_live/live)."""
    adapter = _adapter
    if adapter is None:
        return

    async def on_trip() -> None:
        """Breaker tripped: best-effort flatten via the router's mass-cancel.

        The router is built with BOTH broker adapters so the sweep can cancel
        every open order regardless of which broker placed it.
        """
        async with _trip_lock:
            router = OrderRouter(
                settings=settings,
                pool=get_pool(),
                redis=get_redis(),
                liberator_adapter=get_liberator_adapter(),
                settrade_adapter=adapter,
            )
            cancelled, failed = await router.mass_cancel()
            logger.warning(
                "settrade breaker trip mass-cancel: %d cancelled, %d failed",
                len(cancelled),
                len(failed),
            )

    _tasks.append(
        asyncio.create_task(
            heartbeat_loop(
                adapter,
                interval_seconds=settings.settrade_heartbeat_interval_seconds,
                on_trip=on_trip,
            ),
            name="settrade-heartbeat",
        )
    )
    if settings.stage in _RECONCILE_STAGES:
        reconciler = SettradeReconciler(
            adapter,
            interval_seconds=settings.settrade_reconcile_interval_seconds,
            pool_provider=get_pool,
        )
        _tasks.append(asyncio.create_task(reconciler.run(), name="settrade-reconciler"))
    logger.info(
        "settrade workers started (heartbeat=%ds, reconciler=%s)",
        settings.settrade_heartbeat_interval_seconds,
        "on" if settings.stage in _RECONCILE_STAGES else "off",
    )


async def close_settrade_runtime() -> None:
    """Cancel workers, close the client, clear the singleton (idempotent)."""
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
        logger.info("settrade runtime closed")

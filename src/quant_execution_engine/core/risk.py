"""PTRM pre-trade risk gate (D11): caps checked before any venue I/O.

Caps: max order quantity, max order notional, per-second order rate, and a
duplicate-burst window catching *different* ``client_order_id``\\ s carrying the
same economic order (the id-level dedupe already caught identical resends).

Redis failure policy is stage-aware: fail-open with a WARNING in ``sim|paper``
(no real money at risk), fail-closed in ``micro_live|live``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from src.quant_execution_engine.cache.counters import incr_with_ttl
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Stage
from src.quant_execution_engine.contracts.errors import RiskRejected
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

_LIVE_STAGES = frozenset({Stage.MICRO_LIVE, Stage.LIVE})


def _burst_key(order: NormalizedOrder) -> str:
    """Hash the economic identity so the account never appears in Redis keys."""
    raw = f"{order.account}|{order.symbol}|{order.side}|{order.quantity}"
    return f"exe:burst:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


class RiskGate:
    """Stateless caps + Redis-windowed throttles."""

    def __init__(self, settings: Settings, redis: Any | None) -> None:
        self._settings = settings
        self._redis = redis

    async def check(self, order: NormalizedOrder) -> None:
        """Raise :class:`RiskRejected` when any cap is violated."""
        s = self._settings
        if order.quantity > s.risk_max_order_qty:
            raise RiskRejected(
                f"quantity {order.quantity} exceeds max_order_qty {s.risk_max_order_qty}",
                cap="max_order_qty",
                client_order_id=order.client_order_id,
                detail={"limit": s.risk_max_order_qty},
            )
        basis = order.price if order.price is not None else order.stop_price
        if basis is not None:
            notional = basis * order.quantity
            if notional > s.risk_max_order_value:
                raise RiskRejected(
                    f"notional {notional} exceeds max_order_value {s.risk_max_order_value}",
                    cap="max_order_value",
                    client_order_id=order.client_order_id,
                    detail={"limit": str(s.risk_max_order_value)},
                )
        else:
            logger.warning(
                "unpriced %s order %s: notional cap skipped (quantity cap binds)",
                order.order_type,
                order.client_order_id,
            )
        await self._windowed_checks(order)

    async def _windowed_checks(self, order: NormalizedOrder) -> None:
        s = self._settings
        if self._redis is None:
            self._risk_backend_down(order, reason="redis client not configured")
            return
        try:
            rate = await incr_with_ttl(self._redis, f"exe:rate:{int(time.time())}", ttl_seconds=2)
            burst = await incr_with_ttl(
                self._redis, _burst_key(order), s.risk_duplicate_burst_window_seconds
            )
        except Exception as exc:  # noqa: BLE001 - stage-aware degrade
            self._risk_backend_down(order, reason=str(exc))
            return
        if rate > s.risk_max_orders_per_second:
            raise RiskRejected(
                f"order rate exceeds {s.risk_max_orders_per_second}/s",
                cap="rate_limit",
                client_order_id=order.client_order_id,
                detail={"limit": s.risk_max_orders_per_second},
            )
        if burst > 1:
            raise RiskRejected(
                "duplicate economic order within the burst window",
                cap="duplicate_burst",
                client_order_id=order.client_order_id,
                detail={"window_seconds": s.risk_duplicate_burst_window_seconds},
            )

    def _risk_backend_down(self, order: NormalizedOrder, *, reason: str) -> None:
        """Fail-open in sim/paper; fail-closed where real money is reachable."""
        if self._settings.stage in _LIVE_STAGES:
            raise RiskRejected(
                "risk backend unavailable; refusing to route in a live stage",
                cap="risk_backend_down",
                client_order_id=order.client_order_id,
            )
        logger.warning(
            "risk backend unavailable (%s); rate/burst caps skipped in stage %s",
            reason,
            self._settings.stage,
        )

"""PTRM pre-trade risk gate (D11): caps checked before any venue I/O.

Caps: max order quantity, max order notional, per-second order rate, and a
duplicate-burst window catching *different* ``client_order_id``\\ s carrying the
same economic order (the id-level dedupe already caught identical resends).

Per-account caps (Phase 6 / A1): ``order.account`` is looked up in the
per-account qty/notional maps; a present account binds to its own cap, an absent
account falls back to the global cap (never a silent skip). Enforced in EVERY
stage, including ``sim`` — the cap checks are not mode-dependent.

The duplicate-burst guard (Phase 6 / A3) is the single, unified guard: a richer
fingerprint (``account|symbol|side|quantity|order_type|price``), a configurable
window, gated by ``duplicate_burst_guard_enabled`` (default on), raising the
typed :class:`DuplicateBurstDetected` (409) — distinct from the per-second rate
cap (429), which keeps running even when the burst guard is disabled.

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
from src.quant_execution_engine.contracts.errors import DuplicateBurstDetected, RiskRejected
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

_LIVE_STAGES = frozenset({Stage.MICRO_LIVE, Stage.LIVE})


def _burst_key(order: NormalizedOrder) -> str:
    """Hash the economic identity so the account never appears in Redis keys.

    The fingerprint includes ``order_type`` and ``price`` (Phase 6 / A3) so a
    legitimate re-price is no longer over-blocked while exact economic
    duplicates still trip. ``price`` is ``None`` for MARKET-style orders → the
    literal ``"None"`` (a distinct fingerprint from any priced order). The cid is
    deliberately excluded: id-level dedupe runs earlier in the router, so the
    burst check only ever sees post-dedupe (different-cid) orders.
    """
    price = "None" if order.price is None else format(order.price, "f")
    raw = f"{order.account}|{order.symbol}|{order.side}|{order.quantity}|{order.order_type}|{price}"
    return f"exe:burst:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


class RiskGate:
    """Stateless caps + Redis-windowed throttles."""

    def __init__(self, settings: Settings, redis: Any | None) -> None:
        self._settings = settings
        self._redis = redis

    async def check(self, order: NormalizedOrder) -> None:
        """Raise on any cap violation; checked before any venue I/O (all stages)."""
        self._check_quantity(order)
        self._check_notional(order)
        await self._windowed_checks(order)

    # ----------------------------------------------------------- per-order caps
    def _check_quantity(self, order: NormalizedOrder) -> None:
        """Quantity cap: per-account when configured, else the global cap."""
        limit = self._settings.account_max_qty.get(order.account, self._settings.risk_max_order_qty)
        if order.quantity > limit:
            raise RiskRejected(
                f"quantity {order.quantity} exceeds max_order_qty {limit}",
                cap=self._qty_cap_name(order),
                client_order_id=order.client_order_id,
                detail={"limit": limit},
            )

    def _check_notional(self, order: NormalizedOrder) -> None:
        """Notional cap: per-account when configured, else the global cap.

        The basis is ``price`` (or ``stop_price`` for stop orders); an unpriced
        order has no notional, so only the quantity cap binds (logged).
        """
        basis = order.price if order.price is not None else order.stop_price
        if basis is None:
            logger.warning(
                "unpriced %s order %s: notional cap skipped (quantity cap binds)",
                order.order_type,
                order.client_order_id,
            )
            return
        limit = self._settings.account_max_notional.get(
            order.account, self._settings.risk_max_order_value
        )
        notional = basis * order.quantity
        if notional > limit:
            raise RiskRejected(
                f"notional {notional} exceeds max_order_value {limit}",
                cap=self._notional_cap_name(order),
                client_order_id=order.client_order_id,
                detail={"limit": str(limit)},
            )

    def _qty_cap_name(self, order: NormalizedOrder) -> str:
        return (
            "account_max_qty"
            if order.account in self._settings.account_max_qty
            else "max_order_qty"
        )

    def _notional_cap_name(self, order: NormalizedOrder) -> str:
        return (
            "account_max_notional"
            if order.account in self._settings.account_max_notional
            else "max_order_value"
        )

    # ----------------------------------------------------------- windowed caps
    async def _windowed_checks(self, order: NormalizedOrder) -> None:
        s = self._settings
        if self._redis is None:
            self._risk_backend_down(order, reason="redis client not configured")
            return
        guard_on = s.duplicate_burst_guard_enabled
        try:
            rate = await incr_with_ttl(self._redis, f"exe:rate:{int(time.time())}", ttl_seconds=2)
            burst = (
                await incr_with_ttl(
                    self._redis, _burst_key(order), s.duplicate_burst_window_seconds
                )
                if guard_on
                else 0
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
        if guard_on and burst > 1:
            raise DuplicateBurstDetected(
                "duplicate economic order within the burst window",
                client_order_id=order.client_order_id,
                detail={"window_seconds": s.duplicate_burst_window_seconds},
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

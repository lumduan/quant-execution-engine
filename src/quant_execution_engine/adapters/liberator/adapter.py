"""``LiberatorAdapter`` — the first real venue, composed over HTTP (D9).

Implements the frozen 7-method ``BrokerAdapter`` interface plus the
``HeartbeatHook`` probe and the reconciler-facing ``fetch_venue_orders`` read
(precedent: ``SimAdapter.heartbeat`` already extends beyond the frozen seven).

Boundaries the adapter keeps:

* It never persists — the router/reconciler own the durable lifecycle; the
  adapter only translates wire shapes and reports acks/venue truth.
* It never re-implements Liberator (auth, OTP, sessions stay upstream; D10) —
  a dead session surfaces through ``heartbeat()``.
* A venue rejection is never swallowed: it travels as a rejected ack carrying
  the venue's own text (``errorCode``/``errMsg``/``rejectCode``).
* A transport failure on ``place`` propagates AFTER the durable PENDING_NEW
  insert — that is the designed lost-ack window the reconciler repairs (§B).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import SecretStr

from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.adapters.liberator import mapping
from src.quant_execution_engine.adapters.liberator.errors import (
    LiberatorMappingError,
    LiberatorTransportError,
)
from src.quant_execution_engine.adapters.liberator.models import (
    VenueOrderItem,
    parse_order_items,
)
from src.quant_execution_engine.adapters.liberator.transport import LiberatorTransport
from src.quant_execution_engine.adapters.rate_limit import TokenBucket
from src.quant_execution_engine.adapters.session import SessionCircuitBreaker
from src.quant_execution_engine.contracts.capabilities import (
    CAPABILITY_MATRIX,
    CapabilitySet,
)
from src.quant_execution_engine.contracts.enums import Broker, Market
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

# Resolves a client_order_id to its persisted (broker_order_id, market) —
# injected by the runtime so the adapter never imports the db layer.
OrderIdResolver = Callable[[str], Awaitable[tuple[str, Market] | None]]

_HEARTBEAT_PATH = "order/health/set"
_PORTFOLIO_PATH = "portfolio/get"


class LiberatorAdapter(BrokerAdapter):
    """Routes normalized orders to the bundled liberator-trading-api upstream."""

    broker: ClassVar[Broker] = Broker.LIBERATOR

    def __init__(
        self,
        *,
        transport: LiberatorTransport,
        pin: SecretStr,
        breaker_threshold: int = 3,
        post_rate_limit: float = 5.0,
        resolve_order: OrderIdResolver | None = None,
    ) -> None:
        super().__init__()
        self.breaker = SessionCircuitBreaker(failure_threshold=breaker_threshold)
        self._transport = transport
        self._pin = pin
        # Venue-facing placement cap (Phase 6 / D2): a token bucket on the
        # placement path ONLY. cancel()/heartbeat()/reconciler fetches stay
        # unthrottled — a cancel or a liveness probe must never queue behind a
        # placement burst. ``rate <= 0`` disables it.
        self._place_limiter = TokenBucket(post_rate_limit, name="liberator_post")
        self._resolve_order = resolve_order
        # cid -> (orderNo, market); warm path for cancels of orders this
        # process placed. The injected resolver is the durable fallback.
        self._order_no_cache: dict[str, tuple[str, Market]] = {}
        self.last_heartbeat_ok: bool | None = None

    # ------------------------------------------------------------------ place
    async def place(self, order: NormalizedOrder) -> PlaceAck:
        try:
            payload = mapping.to_place_payload(order, pin=self._pin.get_secret_value())
        except LiberatorMappingError as exc:
            return PlaceAck(rejected=True, reject_reason=str(exc))
        # Throttle the placement path to the venue's request budget (D2) — a
        # mapping-rejected order never reaches here, so it consumes no token. On
        # exhaustion this awaits (back-pressure); it never drops or raises.
        await self._place_limiter.acquire()
        envelope = await self._transport.post(mapping.place_path(order.market), payload)
        if not envelope.ok:
            return PlaceAck(rejected=True, reject_reason=envelope.reject_reason())
        order_no = envelope.order_no()
        if order_no is None:
            raise AdapterError("liberator ack carried no orderNo (data.result.orderNo)")
        self._order_no_cache[order.client_order_id] = (order_no, order.market)
        # Fills arrive via the reconciliation loop in v1 (no per-fill ack data).
        return PlaceAck(broker_order_id=order_no, fills=())

    # ----------------------------------------------------------------- cancel
    async def cancel(self, client_order_id: str) -> CancelAck:
        resolved = self._order_no_cache.get(client_order_id)
        if resolved is None and self._resolve_order is not None:
            resolved = await self._resolve_order(client_order_id)
        if resolved is None:
            return CancelAck(ok=False, reason="no broker_order_id mapping for client_order_id")
        order_no, market = resolved
        payload = mapping.to_cancel_payload(order_no, pin=self._pin.get_secret_value())
        try:
            envelope = await self._transport.post(mapping.cancel_path(market), payload)
        except LiberatorTransportError as exc:
            # Router keeps PENDING_CANCEL; the reconciler resolves it (§B).
            return CancelAck(ok=False, reason=str(exc))
        if not envelope.ok:
            return CancelAck(ok=False, reason=envelope.reject_reason())
        return CancelAck(ok=True)

    # ------------------------------------------------------------------ amend
    async def amend(
        self,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        """Liberator has NO amend route (R2) — declared, never faked.

        The real cancel+replace orchestration (old id ends CANCELLED, a new
        client_order_id starts PENDING_NEW) lives in ``OrderRouter.amend``;
        this frozen-interface slot only declares the semantics.
        """
        return AmendAck(
            ok=False,
            semantics="cancel_replace",
            reason=(
                "liberator has no amend route; use the router cancel+replace "
                "orchestration with a new client_order_id"
            ),
        )

    # ------------------------------------------------------------------ reads
    async def fetch_venue_orders(self, account: str) -> list[VenueOrderItem]:
        """Raw venue order rows — the reconciler's polling source (ADR §B)."""
        body = await self._transport.get_json(mapping.orders_path(account))
        return parse_order_items(body)

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        """Venue-truth open orders as a read-only normalized view.

        Rows the frozen contract cannot represent are skipped (never guessed);
        client ids are deterministic placeholders — see ``mapping``.
        """
        items = await self.fetch_venue_orders(account)
        normalized: list[NormalizedOrder] = []
        for item in items:
            if mapping.classify_venue_state(item) is not mapping.VenueOrderState.RESTING:
                continue
            if item.balance <= 0:
                continue  # fully matched/cancelled rows are not open
            view = mapping.venue_item_to_normalized(item, account=account)
            if view is not None:
                normalized.append(view)
        return normalized

    async def get_positions(self, account: str) -> list[Position]:
        """Portfolio positions (v1: the equities portfolio — market SET)."""
        body = await self._transport.get_json(f"{_PORTFOLIO_PATH}/{account}")
        data = body.get("data")
        if not isinstance(data, dict):
            return []
        raw_positions = data.get("positions")
        if not isinstance(raw_positions, list):
            return []
        positions: list[Position] = []
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue
            symbol = raw.get("symbol")
            quantity = raw.get("quantity")
            if not isinstance(symbol, str) or not isinstance(quantity, int):
                continue
            positions.append(
                Position(account=account, market=Market.SET, symbol=symbol, net_qty=quantity)
            )
        return positions

    async def get_account(self, account: str) -> AccountInfo:
        """Buying power from the portfolio summary (0 when unavailable)."""
        body = await self._transport.get_json(f"{_PORTFOLIO_PATH}/{account}")
        buying_power = Decimal("0")
        data = body.get("data")
        if isinstance(data, dict):
            summary = data.get("summary")
            if isinstance(summary, dict):
                raw = summary.get("buying_power")
                # Upstream serializes Decimal as a JSON number; str() round-trip
                # is the boundary conversion (never keep the float).
                if isinstance(raw, str | int | float) and not isinstance(raw, bool):
                    buying_power = Decimal(str(raw))
        return AccountInfo(account=account, buying_power=buying_power)

    # ------------------------------------------------------------- liveness
    async def heartbeat(self) -> bool:
        """Low-impact session probe (ADR §G): no venue round-trip, no PIN.

        Healthy ⇔ HTTP 200 ∧ status=="healthy" ∧ auth_token_available — the
        last term is exactly the dead-broker-session signal. Never raises.
        """
        try:
            body: dict[str, Any] = await self._transport.get_json(_HEARTBEAT_PATH)
        except LiberatorTransportError as exc:
            logger.warning("liberator heartbeat failed: %s", exc)
            self.last_heartbeat_ok = False
            return False
        ok = bool(body.get("status") == "healthy" and body.get("auth_token_available"))
        self.last_heartbeat_ok = ok
        return ok

    # ------------------------------------------------------------------ meta
    def capabilities(self) -> tuple[CapabilitySet, ...]:
        return tuple(entry for entry in CAPABILITY_MATRIX if entry.broker is Broker.LIBERATOR)

    async def aclose(self) -> None:
        await self._transport.aclose()

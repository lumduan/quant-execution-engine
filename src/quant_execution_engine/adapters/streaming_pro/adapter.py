"""``StreamingProAdapter`` — the third real venue, composed over HTTP (D9).

Implements the frozen 7-method ``BrokerAdapter`` interface plus a ``heartbeat()`` probe and the
reconciler-facing ``fetch_venue_orders`` read (precedent: Liberator). Boundaries:

* It never persists — the router/reconciler own the durable lifecycle.
* It never re-implements the bridge (login/OTP/session/PIN stay in the bridge; D10) — a dead retail
  session surfaces through ``heartbeat()`` (the bridge's ``/session/status``).
* It stamps **no PIN** — the bridge owns USERNAME/PASSWORD/PIN and stamps the PIN itself.
* A bridge/venue rejection is never swallowed: it travels as a rejected ack carrying the reason.
* SET cancel needs ``ext_order_no`` + ``symbol`` (TFEX needs only ``order_no``); the place response
  carries only ``order_no``, so SET cancel resolves ``ext_order_no`` via a ``GET /orders`` lookup.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, ClassVar

from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AccountType,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.adapters.rate_limit import TokenBucket
from src.quant_execution_engine.adapters.session import SessionCircuitBreaker
from src.quant_execution_engine.adapters.streaming_pro import mapping
from src.quant_execution_engine.adapters.streaming_pro.errors import (
    StreamingProAccountUnavailable,
    StreamingProMappingError,
    StreamingProTransportError,
)
from src.quant_execution_engine.adapters.streaming_pro.models import (
    BridgePlace,
    VenueOrderRow,
    parse_order_rows,
)
from src.quant_execution_engine.adapters.streaming_pro.transport import StreamingProTransport
from src.quant_execution_engine.contracts.capabilities import CAPABILITY_MATRIX, CapabilitySet
from src.quant_execution_engine.contracts.enums import Broker, Market
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

# cid -> (order_no, market, account, symbol) — the durable resolver the cancel falls back to.
OrderRef = tuple[str, Market, str, str]
OrderIdResolver = Callable[[str], Awaitable[OrderRef | None]]

_HEARTBEAT_PATH = "session/status"


def _opt_decimal(body: dict[str, Any], key: str) -> Decimal | None:
    """A money field, or ``None`` when absent/unusable — never a sentinel zero."""
    raw = body.get(key)
    if isinstance(raw, bool) or not isinstance(raw, str | int | float):
        return None
    return Decimal(str(raw))


class StreamingProAdapter(BrokerAdapter):
    """Routes normalized orders to the bundled settrade-streaming-api bridge over HTTP."""

    broker: ClassVar[Broker] = Broker.STREAMING_PRO

    def __init__(
        self,
        *,
        transport: StreamingProTransport,
        breaker_threshold: int = 3,
        post_rate_limit: float = 5.0,
        resolve_order: OrderIdResolver | None = None,
    ) -> None:
        super().__init__()
        self.breaker = SessionCircuitBreaker(failure_threshold=breaker_threshold)
        self._transport = transport
        # Venue-facing placement cap (D2): a token bucket on place() ONLY — cancel/heartbeat/
        # reconciler reads stay unthrottled. ``rate <= 0`` disables it.
        self._place_limiter = TokenBucket(post_rate_limit, name="streaming_pro_post")
        self._resolve_order = resolve_order
        self._order_ref_cache: dict[str, OrderRef] = {}
        self.last_heartbeat_ok: bool | None = None

    # ------------------------------------------------------------------ place
    async def place(self, order: NormalizedOrder) -> PlaceAck:
        try:
            payload = mapping.to_place_payload(order)
        except StreamingProMappingError as exc:
            return PlaceAck(rejected=True, reject_reason=str(exc))
        await self._place_limiter.acquire()  # back-pressure on exhaustion; never drops/raises
        body = await self._transport.post(mapping.place_path(order.market), payload)
        place = BridgePlace.model_validate(body)
        if not place.ok or place.order_no is None:
            return PlaceAck(rejected=True, reject_reason=place.reject_reason())
        self._order_ref_cache[order.client_order_id] = (
            place.order_no,
            order.market,
            order.account,
            order.symbol,
        )
        # Fills arrive via the reconciliation loop in v1 (no per-fill ack data).
        return PlaceAck(broker_order_id=place.order_no, fills=())

    # ----------------------------------------------------------------- cancel
    async def cancel(self, client_order_id: str) -> CancelAck:
        ref = self._order_ref_cache.get(client_order_id)
        if ref is None and self._resolve_order is not None:
            ref = await self._resolve_order(client_order_id)
        if ref is None:
            return CancelAck(ok=False, reason="no broker_order_id mapping for client_order_id")
        order_no, market, account, symbol = ref
        ext_order_no: str | None = None
        if market is Market.SET:
            # SET cancel needs extOrderNo (absent from the place ack) — resolve via the order book.
            ext_order_no = await self._lookup_ext_order_no(account, market, order_no)
        payload = mapping.to_cancel_payload(
            order_no=order_no,
            market=market,
            account=account,
            symbol=symbol,
            ext_order_no=ext_order_no,
        )
        try:
            body = await self._transport.post(mapping.cancel_path(), payload)
        except StreamingProTransportError as exc:
            # Router keeps PENDING_CANCEL; the reconciler resolves it (§B).
            return CancelAck(ok=False, reason=str(exc))
        if body.get("ok") is not True:
            return CancelAck(ok=False, reason=BridgePlace.model_validate(body).reject_reason())
        return CancelAck(ok=True)

    async def _lookup_ext_order_no(self, account: str, market: Market, order_no: str) -> str | None:
        try:
            rows = await self.fetch_venue_orders(account, market)
        except StreamingProTransportError:
            return None
        return next(
            (r.ext_order_no for r in rows if r.order_no == order_no and r.ext_order_no), None
        )

    # ------------------------------------------------------------------ amend
    async def amend(
        self,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        """The bridge's native amend is capture-pending (``/order/change`` 501) — declared.

        The real cancel+replace orchestration (old id ends CANCELLED, a new client_order_id starts
        PENDING_NEW) lives in ``OrderRouter.amend``; this frozen slot only declares the semantics.
        """
        return AmendAck(
            ok=False,
            semantics="cancel_replace",
            reason=(
                "streaming_pro native amend not yet captured; use the router cancel+replace "
                "orchestration with a new client_order_id"
            ),
        )

    # ------------------------------------------------------------------ reads
    async def fetch_venue_orders(self, account: str, market: Market) -> list[VenueOrderRow]:
        """Raw venue order rows for one market — the reconciler's polling source (ADR §B)."""
        body = await self._transport.get_json(mapping.orders_path(account, market))
        return parse_order_rows(body)

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        """Venue-truth open orders (both markets) as a read-only normalized view."""
        normalized: list[NormalizedOrder] = []
        for market in (Market.SET, Market.TFEX):
            rows = await self.fetch_venue_orders(account, market)
            for row in rows:
                if mapping.classify_venue_state(row) is not mapping.VenueOrderState.RESTING:
                    continue
                if row.balance <= 0:
                    continue  # fully matched/cancelled rows are not open
                view = mapping.venue_row_to_normalized(row, account=account, market=market)
                if view is not None:
                    normalized.append(view)
        return normalized

    async def get_positions(self, account: str) -> list[Position]:
        """Portfolio positions (v1: the equities portfolio — market SET; TFEX is a follow-up)."""
        body = await self._transport.get_json(mapping.portfolio_path(account))
        raw_positions = body.get("positions") if isinstance(body, dict) else None
        if not isinstance(raw_positions, list):
            return []
        positions: list[Position] = []
        for raw in raw_positions:
            if not isinstance(raw, dict):
                continue
            symbol = raw.get("symbol")
            qty = _coerce_int(raw.get("currentVolume", raw.get("actualVolume")))
            if isinstance(symbol, str) and qty is not None:
                positions.append(
                    Position(account=account, market=Market.SET, symbol=symbol, net_qty=qty)
                )
        return positions

    async def get_account(self, account: str) -> AccountInfo:
        """Balance from ``account-info``.

        ⚠️ **SET-only.** ``account_service.py`` hardcodes the ``fis`` segment, so a TFEX
        account reaches the equity front and comes back unknown. There is no margin block:
        SP reports none, which is why those fields stay ``None`` and ``account_type`` is CASH.

        Raises :class:`StreamingProAccountUnavailable` when the body carries no balance.
        ⚠️ It previously returned ``Decimal("0")`` here — the last surviving instance of the
        silent-degrade [[TK-0396]] removed everywhere else. A zero for an unreadable account
        is indistinguishable from a real zero.
        """
        body = await self._transport.get_json(mapping.account_path(account))
        if not isinstance(body, dict):
            raise StreamingProAccountUnavailable(
                f"streaming_pro account-info for {account!r} returned no object"
            )
        buying_power = _opt_decimal(body, "lineAvailable")
        if buying_power is None:
            raise StreamingProAccountUnavailable(
                f"streaming_pro account-info for {account!r} carried no lineAvailable "
                f"(keys: {sorted(body)[:8]}) — the venue reports this account as unknown "
                "when the account is not on the SET/`fis` front"
            )
        return AccountInfo(
            account=account,
            account_type=AccountType.CASH,
            buying_power=buying_power,
            cash_balance=_opt_decimal(body, "cashBalance"),
            credit_limit=_opt_decimal(body, "creditLimit"),
        )

    # ------------------------------------------------------------- liveness
    async def heartbeat(self) -> bool:
        """Retail-session liveness via the bridge's ``/session/status`` (ADR §G). Never raises."""
        try:
            body: Any = await self._transport.get_json(_HEARTBEAT_PATH)
        except StreamingProTransportError as exc:
            logger.warning("streaming_pro heartbeat failed: %s", exc)
            self.last_heartbeat_ok = False
            return False
        ok = bool(isinstance(body, dict) and (body.get("alive") or body.get("status") == "alive"))
        self.last_heartbeat_ok = ok
        return ok

    # ------------------------------------------------------------------ meta
    def capabilities(self) -> tuple[CapabilitySet, ...]:
        return tuple(e for e in CAPABILITY_MATRIX if e.broker is Broker.STREAMING_PRO)

    async def aclose(self) -> None:
        await self._transport.aclose()


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str | float):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return None

"""``SettradeAdapter`` — the second real venue, re-implemented on the wire (D2).

Implements the frozen 7-method ``BrokerAdapter`` interface plus the
``HeartbeatHook`` probe and the reconciler-facing ``fetch_venue_orders`` read
(precedent: ``LiberatorAdapter``). Settrade differs from Liberator in every
dimension that matters: OAuth app-credentials (not OTP/PIN), **native** order
amendment (not cancel-then-replace), and full SET equity + TFEX derivatives
coverage across two order books.

Boundaries the adapter keeps (mirrors Liberator):

* It never persists — the router/reconciler own the durable lifecycle; the
  adapter only translates wire shapes and reports acks/venue truth.
* It never re-implements Settrade's session (OAuth lives in ``client.py``) —
  a dead session surfaces through ``heartbeat()``.
* A venue rejection is never swallowed: it travels as a rejected/not-ok ack
  carrying the venue's own ``{code, message}``; the venue can even return a
  *rejected order object* with a 2xx, and that too becomes a rejected ack.
* A transport failure on ``place`` propagates AFTER the durable PENDING_NEW
  insert — the designed lost-ack window the reconciler repairs (§B).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import ClassVar

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
from src.quant_execution_engine.adapters.session import SessionCircuitBreaker
from src.quant_execution_engine.adapters.settrade import mapping
from src.quant_execution_engine.adapters.settrade.client import SettradeClient
from src.quant_execution_engine.adapters.settrade.errors import (
    SettradeMappingError,
    SettradeTransportError,
    SettradeVenueRejection,
)
from src.quant_execution_engine.adapters.settrade.models import (
    SettradeAccountInfo,
    SettradeOrderItem,
    SettradePlaceResponse,
    SettradePortfolioItem,
    parse_order_items,
)
from src.quant_execution_engine.contracts.capabilities import (
    CAPABILITY_MATRIX,
    CapabilitySet,
)
from src.quant_execution_engine.contracts.enums import Broker, Market
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

# Resolves a client_order_id to its persisted (broker_order_id, market, account)
# — injected by the runtime so the adapter never imports the db layer. Settrade
# carries the account alongside the order number because every Settrade URL is
# account-scoped (``.../accounts/{account}/orders/{order_no}``).
SettradeOrderIdResolver = Callable[[str], Awaitable[tuple[str, Market, str] | None]]

# Both order books — reads (open orders / positions) fan out across them.
_MARKETS: tuple[Market, ...] = (Market.SET, Market.TFEX)


class SettradeAdapter(BrokerAdapter):
    """Routes normalized orders to the Settrade Open API v2 cloud venue."""

    broker: ClassVar[Broker] = Broker.SETTRADE

    def __init__(
        self,
        *,
        client: SettradeClient,
        broker_id: str,
        pin: SecretStr,
        breaker_threshold: int = 3,
        resolve_order: SettradeOrderIdResolver | None = None,
    ) -> None:
        super().__init__()
        self.breaker = SessionCircuitBreaker(failure_threshold=breaker_threshold)
        self._client = client
        self._broker_id = broker_id
        self._pin = pin
        self._resolve_order = resolve_order
        # cid -> (orderNo, market, account); warm path for cancels/amends of
        # orders this process placed. The injected resolver is the durable
        # fallback (URLs are account-scoped, so the account rides the ref).
        self._order_ref_cache: dict[str, tuple[str, Market, str]] = {}
        self.last_heartbeat_ok: bool | None = None

    # ------------------------------------------------------------------ place
    async def place(self, order: NormalizedOrder) -> PlaceAck:
        try:
            payload = mapping.to_place_payload(order, pin=self._pin.get_secret_value())
        except SettradeMappingError as exc:
            return PlaceAck(rejected=True, reject_reason=f"settrade mapping: {exc}")
        path = mapping.orders_path(self._broker_id, order.account, order.market)
        try:
            body = await self._client.post_json(path, payload)
        except SettradeVenueRejection as exc:
            # Venue truth is never swallowed — it travels as a rejected ack.
            return PlaceAck(rejected=True, reject_reason=f"settrade {exc.venue_code}: {exc}")
        # Transport errors PROPAGATE here (the designed lost-ack window §B).
        # The 2xx body is the full order object: it may be a REJECTED order
        # (the venue can stamp a reject on a 2xx response).
        if isinstance(body, dict):
            item = self._as_order_item(body)
            if item is not None and item.rejected:
                reason = item.reject_reason or item.reject_code or "rejected"
                return PlaceAck(rejected=True, reject_reason=f"settrade reject: {reason}")
        order_no = self._extract_order_no(body)
        if order_no is None:
            raise AdapterError("settrade place ack carried no orderNo")
        self._order_ref_cache[order.client_order_id] = (order_no, order.market, order.account)
        # Fills arrive via the reconciliation loop in v1 (no per-fill ack data).
        return PlaceAck(broker_order_id=order_no, fills=())

    @staticmethod
    def _as_order_item(body: dict[str, object]) -> SettradeOrderItem | None:
        try:
            return SettradeOrderItem.model_validate(body)
        except ValueError:
            return None

    @staticmethod
    def _extract_order_no(body: object) -> str | None:
        if not isinstance(body, dict):
            return None
        try:
            return SettradePlaceResponse.model_validate(body).order_no
        except ValueError:
            return None

    # ----------------------------------------------------------------- cancel
    async def cancel(self, client_order_id: str) -> CancelAck:
        ref = await self._resolve_ref(client_order_id)
        if ref is None:
            return CancelAck(ok=False, reason=f"unknown broker order id for {client_order_id}")
        order_no, market, account = ref
        path = mapping.cancel_path(self._broker_id, account, market, order_no)
        cancel_payload = mapping.to_cancel_payload(self._pin.get_secret_value())
        try:
            await self._client.patch_json(path, cancel_payload)
        except SettradeVenueRejection as exc:
            # Router keeps PENDING_CANCEL; the reconciler resolves it (§B). An
            # already-terminal order surfaces here as venue rejection text too.
            return CancelAck(ok=False, reason=f"settrade {exc.venue_code}: {exc}")
        except SettradeTransportError as exc:
            return CancelAck(ok=False, reason=str(exc))
        return CancelAck(ok=True)

    # ------------------------------------------------------------------ amend
    async def amend(
        self,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        """Native amend over Settrade's real change endpoint (PENDING_REPLACE).

        NEVER raises — the router's non-terminal restore path depends on a
        not-ok ack rather than an exception. A venue reject (e.g. a partial fill
        raced the amend) returns ``ok=False`` carrying the venue text; the
        router then restores the order to NEW (+ PARTIALLY_FILLED) and raises
        ``AmendRejected`` itself.
        """
        ref = await self._resolve_ref(client_order_id)
        if ref is None:
            return AmendAck(
                ok=False,
                semantics="native",
                reason=f"unknown broker order id for {client_order_id}",
            )
        order_no, market, account = ref
        try:
            payload = mapping.to_change_payload(
                market,
                pin=self._pin.get_secret_value(),
                new_price=new_price,
                new_qty=new_qty,
            )
        except SettradeMappingError as exc:
            return AmendAck(ok=False, semantics="native", reason=f"settrade mapping: {exc}")
        path = mapping.change_path(self._broker_id, account, market, order_no)
        try:
            await self._client.patch_json(path, payload)
        except SettradeVenueRejection as exc:
            logger.warning("settrade amend rejected for %s: %s", client_order_id, exc.venue_code)
            return AmendAck(
                ok=False, semantics="native", reason=f"settrade {exc.venue_code}: {exc}"
            )
        except SettradeTransportError as exc:
            logger.warning("settrade amend transport failure for %s", client_order_id)
            return AmendAck(ok=False, semantics="native", reason=str(exc))
        logger.info("settrade native amend sent for %s", client_order_id)
        return AmendAck(ok=True, semantics="native")

    async def _resolve_ref(self, client_order_id: str) -> tuple[str, Market, str] | None:
        """Warm cache → injected durable resolver fallback (account-scoped)."""
        ref = self._order_ref_cache.get(client_order_id)
        if ref is None and self._resolve_order is not None:
            ref = await self._resolve_order(client_order_id)
        return ref

    # ------------------------------------------------------------------ reads
    async def fetch_venue_orders(self, account: str, market: Market) -> list[SettradeOrderItem]:
        """Raw venue order rows for one book — the reconciler polling source (§B)."""
        body = await self._client.get_json(mapping.orders_path(self._broker_id, account, market))
        return parse_order_items(body)

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        """Venue-truth open orders across BOTH books as a read-only view.

        Rows the frozen contract cannot represent are skipped (never guessed).
        An account may exist on one book only — a per-market venue rejection is
        tolerated (DEBUG + continue); a transport failure propagates.
        """
        normalized: list[NormalizedOrder] = []
        for market in _MARKETS:
            try:
                items = await self.fetch_venue_orders(account, market)
            except SettradeVenueRejection as exc:
                logger.debug("settrade open-orders %s book unavailable: %s", market, exc.venue_code)
                continue
            for item in items:
                if mapping.classify_venue_state(item) is not mapping.VenueOrderState.RESTING:
                    continue
                if item.balance <= 0:
                    continue  # fully matched/cancelled rows are not open
                view = mapping.venue_item_to_normalized(item, account=account, market=market)
                if view is not None:
                    normalized.append(view)
        return normalized

    async def get_positions(self, account: str) -> list[Position]:
        """Net positions across BOTH books (per-market reject tolerated)."""
        positions: list[Position] = []
        for market in _MARKETS:
            try:
                body = await self._client.get_json(
                    mapping.portfolios_path(self._broker_id, account, market)
                )
            except SettradeVenueRejection as exc:
                logger.debug("settrade portfolio %s unavailable: %s", market, exc.venue_code)
                continue
            for item in self._parse_portfolio(body):
                positions.append(
                    Position(
                        account=account,
                        market=market,
                        symbol=item.symbol,
                        net_qty=item.net_quantity,
                    )
                )
        return positions

    @staticmethod
    def _parse_portfolio(body: object) -> list[SettradePortfolioItem]:
        rows: object = body
        if isinstance(body, dict):
            rows = body.get("data", body.get("portfolios"))
        if not isinstance(rows, list):
            return []
        items: list[SettradePortfolioItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                items.append(SettradePortfolioItem.model_validate(row))
            except ValueError:
                continue
        return items

    async def get_account(self, account: str) -> AccountInfo:
        """Buying power from account-info: equity book first, derivatives fallback.

        The buying-power chain (first non-None wins) is documented per book:
        ``line_available`` (equity credit line) → ``excess_equity`` →
        ``cash_balance`` → ``equity`` → ``Decimal("0")``. The equity book is
        tried first; a per-market venue rejection falls through to derivatives.
        """
        for market in _MARKETS:
            try:
                body = await self._client.get_json(
                    mapping.account_info_path(self._broker_id, account, market)
                )
            except SettradeVenueRejection as exc:
                logger.debug("settrade account-info %s unavailable: %s", market, exc.venue_code)
                continue
            if not isinstance(body, dict):
                continue
            info = self._parse_account_info(body)
            if info is None:
                continue
            return AccountInfo(account=account, buying_power=self._buying_power(info))
        return AccountInfo(account=account, buying_power=Decimal("0"))

    @staticmethod
    def _parse_account_info(body: dict[str, object]) -> SettradeAccountInfo | None:
        payload: object = body.get("data", body)
        if not isinstance(payload, dict):
            return None
        try:
            return SettradeAccountInfo.model_validate(payload)
        except ValueError:
            return None

    @staticmethod
    def _buying_power(info: SettradeAccountInfo) -> Decimal:
        """First non-None of the documented buying-power chain, else zero."""
        for candidate in (info.line_available, info.excess_equity, info.cash_balance, info.equity):
            if candidate is not None:
                return candidate
        return Decimal("0")

    def get_budget_exhausted(self) -> bool:
        """True when the client's GET rate bucket has nothing left (D8).

        The reconciler consults this before polling each ``(account, market)``
        group and budget-skips the rest of the pass when exhausted; observing the
        snapshot keeps the adapter the single owner of the client.
        """
        budget = self._client.rate_snapshot().get("GET")
        if budget is None:
            return False
        return budget.remaining_second == 0 or budget.remaining_minute == 0

    # ------------------------------------------------------------- liveness
    async def heartbeat(self) -> bool:
        """OAuth token-liveness probe (Design Decision 6). NEVER raises.

        Settrade exposes no health/session endpoint, so token *acquirability*
        IS the OAuth session: healthy ⇔ ``ensure_token()`` succeeds AND the last
        real wire call did not fail. This is the E19 analogue (Liberator probes a
        real health route because it has one; Settrade cannot). The residual
        blind spot — a valid token over a dead order endpoint that has seen no
        traffic — is documented and closed in Phase 5 (the MQTT stream gives a
        real session signal).
        """
        try:
            await self._client.ensure_token()
            ok = self._client.last_wire_ok is not False
        except Exception:  # noqa: BLE001 - heartbeat contract: never raises
            logger.warning("settrade heartbeat failed (token unavailable)")
            ok = False
        self.last_heartbeat_ok = ok
        return ok

    # ------------------------------------------------------------------ meta
    def capabilities(self) -> tuple[CapabilitySet, ...]:
        return tuple(entry for entry in CAPABILITY_MATRIX if entry.broker is Broker.SETTRADE)

    async def aclose(self) -> None:
        await self._client.aclose()

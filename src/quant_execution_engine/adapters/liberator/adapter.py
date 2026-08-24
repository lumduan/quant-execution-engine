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
    AccountType,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.adapters.liberator import mapping
from src.quant_execution_engine.adapters.liberator.errors import (
    LiberatorAccountNotFound,
    LiberatorMappingError,
    LiberatorPositionsUncaptured,
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
_PROFILE_PATH = "profile"


def _venue_result(body: Any, *, what: str) -> dict[str, Any]:
    """Unwrap the bridge envelope, refusing on the venue's own error text.

    🔴 Reads ``raw_response.errMsg``, **never** ``success`` — the bridge returns
    ``success: true`` even for an account the venue refused ([[TK-0396]], GH #208).
    ``data`` is ignored because the bridge hard-codes it to ``None`` on both read
    routes; the payload only ever lives under ``raw_response``.
    """
    raw = body.get("raw_response") if isinstance(body, dict) else None
    if not isinstance(raw, dict):
        raise LiberatorTransportError(f"liberator {what}: no raw_response in the envelope")
    err = raw.get("errMsg")
    if isinstance(err, str) and err:
        raise LiberatorAccountNotFound(f"liberator {what}: {err}")
    result = raw.get("result")
    if not isinstance(result, dict):
        raise LiberatorTransportError(f"liberator {what}: raw_response carried no result object")
    return result


def _venue_decimal(value: Any, *, field: str) -> Decimal:
    """Money off this venue, without ever touching ``float`` arithmetic.

    ⚠️ The venue switches JSON type by value — ``lineAvailable`` is a float at
    ``50885.83`` and an **int** at ``0`` — so both must be accepted, and the
    ``str()`` round-trip is the boundary conversion.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise LiberatorTransportError(f"liberator profile: {field} is not a number ({value!r})")
    return Decimal(str(value))


_ACCOUNT_TYPES = {"CASH BALANCE": AccountType.CASH, "DERIVATIVE": AccountType.DERIVATIVE}


def _opt_decimal(raw: dict[str, Any], key: str) -> Decimal | None:
    """A venue money field, or ``None`` when the venue did not send it.

    🔴 ``None``, never ``Decimal("0")`` — absent and zero are different facts and conflating
    them is the whole of [[TK-0396]].
    """
    return None if raw.get(key) is None else _venue_decimal(raw.get(key), field=key)


def _account_info(account: str, raw: dict[str, Any]) -> AccountInfo:
    """Map one ``/va/profile`` ``accounts[]`` entry onto :class:`AccountInfo`.

    The margin block is read ONLY for a DERIVATIVE account. Not an optimisation — the model
    forbids those fields elsewhere, and a cash account never carries them anyway.
    """
    account_type = _ACCOUNT_TYPES.get(str(raw.get("type", "")).upper(), AccountType.UNKNOWN)
    margin: dict[str, Decimal | None] = {}
    if account_type is AccountType.DERIVATIVE:
        margin = {
            "equity": _opt_decimal(raw, "equity"),
            "excess_equity": _opt_decimal(raw, "excessEquity"),
            "initial_margin": _opt_decimal(raw, "totalMr"),
            "maintenance_margin": _opt_decimal(raw, "totalMm"),
        }
    return AccountInfo(
        account=account,
        account_type=account_type,
        buying_power=_venue_decimal(raw.get("lineAvailable"), field="lineAvailable"),
        cash_balance=_opt_decimal(raw, "cashBalance"),
        credit_limit=_opt_decimal(raw, "creditLimit"),
        withdrawable=_opt_decimal(raw, "withdrawAvailable"),
        **margin,
    )


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
        """🔴 Not implementable yet — raises :class:`LiberatorPositionsUncaptured`.

        ``POST /va/portfolio`` answers with ``result.{list, stock}`` and **neither array
        has ever been observed non-empty**: no Liberator account on this platform holds
        a position, so the element schema has never been captured.

        The previous implementation parsed ``data["positions"]`` — a key the bridge does
        not emit — and returned ``[]`` for every account **without raising**. Replacing
        one invented parse with another would repeat the defect, so this refuses loudly
        instead. See ``docs/reference/liberator-account-reads.md`` (umbrella) §7.
        """
        raise LiberatorPositionsUncaptured(
            "liberator positions are not readable: the element schema of "
            "raw_response.result.{list,stock} has never been captured (TK-0396)",
            detail={"account": account, "endpoint": f"{_PORTFOLIO_PATH}/{account}"},
        )

    async def get_account(self, account: str) -> AccountInfo:
        """Buying power for ``account``, from ``GET /va/profile``.

        ⚠️ **Not** from ``portfolio/get``, which is what this used to call: that route
        carries **no balance field in any shape** — its entire payload for a real,
        funded account is ``{"list": [], "stock": []}`` — so it returned ``0`` for
        accounts holding real money ([[TK-0396]]).

        ``lineAvailable`` is the buying-power field. The venue also exposes
        ``cashBalance`` / ``withdrawAvailable`` / ``creditLimit`` and, on derivatives,
        ``equity`` / ``excessEquity`` / ``totalMr`` / ``totalMm``; :class:`AccountInfo`
        carries only buying power today, so the rest is deliberately dropped — see
        ``docs/broker-commands.md`` §7, where growing the contract is an open question.

        Raises :class:`LiberatorAccountNotFound` when ``account`` is not on the profile.
        ⚠️ It does **not** fall back to zero: a ``0`` for an unknown account cannot be
        told apart from a real zero, and that ambiguity is the defect this replaces.
        """
        body = await self._transport.get_json(_PROFILE_PATH)
        result = _venue_result(body, what="profile")
        accounts = result.get("accounts")
        if not isinstance(accounts, list):
            raise LiberatorTransportError("liberator profile: result carried no accounts list")
        for raw in accounts:
            if isinstance(raw, dict) and raw.get("accountNo") == account:
                return _account_info(account, raw)
        raise LiberatorAccountNotFound(
            f"account {account!r} is not on this login's profile "
            "(accounts are 8-digit <login><suffix>; a bare login is not an account)"
        )

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

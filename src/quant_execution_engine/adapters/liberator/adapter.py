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

from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AccountType,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.adapters.liberator import mapping
from src.quant_execution_engine.adapters.liberator.errors import (
    LiberatorAccountNotFound,
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
from src.quant_execution_engine.contracts.enums import Broker, Market, Side
from src.quant_execution_engine.contracts.orders import NormalizedOrder

logger = logging.getLogger(__name__)

# Resolves a client_order_id to its persisted (broker_order_id, market) —
# injected by the runtime so the adapter never imports the db layer.
OrderIdResolver = Callable[[str], Awaitable[tuple[str, Market] | None]]

_HEARTBEAT_PATH = "order/health/set"
_PORTFOLIO_PATH = "portfolio/get"
_PROFILE_PATH = "profile"

# 🔑 sideShow -> Side, from the FIRST populated capture (2026-08-28, umbrella
# `docs/reference/liberator-account-reads.md` §2.2). Every literal here was OBSERVED;
# none is guessed.
#
# ⚠️ SET sends the EMPTY STRING — not a missing key, and not "Long". A SET equity
# cannot be short and the venue declines to say, so `""` -> None is the faithful
# reading. `Position.side is None` means *"the venue did not distinguish"*, never
# *"flat"* and never *"long"*.
#
# Long->BUY / Short->SELL is a CONVENTION (a long position is one that was bought);
# `Side` has only BUY/SELL, so this is the sole available mapping rather than a
# claim that the venue said "BUY".
_POSITION_SIDES: dict[str, Side | None] = {"Long": Side.BUY, "Short": Side.SELL, "": None}

# The venue's own account grammar: 8 digits, `<investorId><suffix>`, suffix 2 = CASH
# BALANCE (SET) and 7 = DERIVATIVE (TFEX). CAPTURED — reference doc §3.
_ACCOUNT_SUFFIX_MARKETS = {"2": Market.SET, "7": Market.TFEX}


def _position_market(account: str) -> Market:
    """Market for a Liberator account, from the venue's documented account grammar.

    🔴 **This is NOT the "infer the market from the account number" anti-pattern**, and
    the difference is worth stating because the same file's sibling adapter does the
    opposite. For **Streaming Pro** the account number carries no market — SET `…97` and
    TFEX `…99` differ by one digit and mean different books — so that adapter must ASK
    the venue which front answers. For **Liberator** the suffix *is* the venue's own
    encoding, captured and documented (§3), and `/va/portfolio` takes **no market
    parameter at all** (§4) — there is nothing to ask.

    ⚠️ The grammar check also closes a false-absence trap: a bare 7-digit ``investorId``
    is accepted by the venue, returns ``errMsg: ""`` and an **empty** result — which is
    indistinguishable from a genuinely flat account. Refusing it here means "I cannot
    read this" never renders as "there is nothing here".
    """
    if len(account) != 8 or not account.isdigit():
        raise LiberatorAccountNotFound(
            f"liberator portfolio: {account!r} is not an 8-digit trading account "
            "(<investorId><suffix>); a bare investorId returns an empty result that "
            "is indistinguishable from a flat account",
            detail={"account": account},
        )
    market = _ACCOUNT_SUFFIX_MARKETS.get(account[-1])
    if market is None:
        raise LiberatorAccountNotFound(
            f"liberator portfolio: account suffix {account[-1]!r} is neither 2 "
            "(CASH BALANCE/SET) nor 7 (DERIVATIVE/TFEX)",
            detail={"account": account},
        )
    return market


def _position(account: str, market: Market, raw: dict[str, Any]) -> Position:
    """One ``result.stock[]`` element -> ``Position``. Parses ONLY observed fields.

    Unknown keys are ignored rather than rejected: the capture proves the element is
    **market-dependent** (17 fields on TFEX, 14 on SET — `optVol`, `positionShow` and
    `startVol` are TFEX-only), and it cannot prove what a *short SET* or an *expiring
    TFEX* row carries. Rejecting an unrecognised key would turn a venue addition into
    an outage.
    """
    symbol = raw.get("symbolDisplay") or raw.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise LiberatorTransportError(
            f"liberator portfolio: position row carries no symbol ({raw!r})"
        )
    qty = raw.get("actualVol")
    if isinstance(qty, bool) or not isinstance(qty, int):
        raise LiberatorTransportError(
            f"liberator portfolio: {symbol} actualVol is not an integer ({qty!r})"
        )
    side_show = raw.get("sideShow")
    if side_show is not None and side_show not in _POSITION_SIDES:
        # Loud, not silent: an unmapped literal means the vocabulary grew, and guessing
        # would put a long leg on the short side of a book.
        raise LiberatorTransportError(
            f"liberator portfolio: unmapped sideShow {side_show!r} for {symbol} "
            f"(known: {sorted(_POSITION_SIDES)})"
        )
    return Position(
        account=account,
        market=market,
        symbol=symbol,
        net_qty=qty,
        side=_POSITION_SIDES.get(side_show or ""),
        # 🔴 READ, NEVER RECOMPUTE — §2.2a. `amount`/`marketVal` already carry the TFEX
        # ×1000 contract multiplier (per-series; never assume 1000), and `avg` is a
        # ROUNDED display value: ORI's amount/qty is 1.79109 against a reported avg of
        # 1.79. Deriving cost from `avg × qty` is off by ฿1.09 on a ฿1,791 position.
        cost_amount=_opt_decimal(raw, "amount"),
        avg_price=_opt_decimal(raw, "avg"),
        market_price=_opt_decimal(raw, "marketPrice"),
        market_value=_opt_decimal(raw, "marketVal"),
        unrealized_pl=_opt_decimal(raw, "unrealizedPL"),
    )


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
    ``50000.11`` and an **int** at ``0`` — so both must be accepted, and the
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
        breaker_threshold: int = 3,
        post_rate_limit: float = 5.0,
        resolve_order: OrderIdResolver | None = None,
    ) -> None:
        super().__init__()
        self.breaker = SessionCircuitBreaker(failure_threshold=breaker_threshold)
        self._transport = transport
        # Venue-facing placement cap (Phase 6 / D2): a token bucket on the
        # placement path ONLY. cancel()/heartbeat()/reconciler fetches stay
        # unthrottled — a cancel or a liveness probe must never queue behind a
        # placement burst. ``rate <= 0`` disables it.
        self._place_limiter = TokenBucket(post_rate_limit, name="liberator_post")
        self._resolve_order = resolve_order
        # cid -> (orderNo, market); warm path for cancels of orders this
        # process placed. The injected resolver is the durable fallback.
        self._order_no_cache: dict[str, tuple[str, Market]] = {}
        # cids the venue ACCEPTED but for which the ack carried no orderNo. Distinct from
        # "unknown order": these are LIVE and must never be resubmitted. Cleared once the
        # reconciler supplies the handle. See the block in `place`.
        self._awaiting_order_no: set[str] = set()
        self.last_heartbeat_ok: bool | None = None

    # ------------------------------------------------------------------ place
    async def place(self, order: NormalizedOrder) -> PlaceAck:
        try:
            payload = mapping.to_place_payload(order)
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
            # 🔴 NEVER RAISE HERE. The venue has ALREADY ACCEPTED this order.
            #
            # This used to `raise AdapterError(...)`, which turned a landed order into an
            # HTTP 500 with an empty body and skipped the `_order_no_cache` write below —
            # so the engine held `broker_order_id=NULL` while the venue held a real number.
            # Observed live on 2026-08-25: the bridge returned 200, the venue numbered the
            # order 10993, and the caller got a 500. A 500 on a path that can retry is a
            # DUPLICATE-ORDER shape, which is far worse than a missing handle.
            #
            # ⚠️ The ordering was the defect, not the missing field: any raise placed after
            # a successful venue write loses the handle to an order that exists. So this
            # returns a TRUTHFUL ack instead — accepted, handle pending — and lets the
            # reconciler's ADR §B lost-ack `fuzzy_match` supply the number on its next pass
            # (it joins on account/symbol/side/qty + entry-time skew, needing no client id).
            #
            # `broker_order_id=None` is the schema's DESIGNED pre-ack state, not a sentinel:
            # `12_schema_execution.sql:83` — "venue-assigned and NULL until ack (ADR §B)".
            self._awaiting_order_no.add(order.client_order_id)
            logger.error(
                "liberator ack carried no orderNo (data.result.orderNo) for %s — the order IS "
                "LIVE at the venue and is reported accepted with broker_order_id=None. The "
                "handle must come from the next reconcile pass; until then it cannot be "
                "cancelled by client_order_id. NOT raising: the venue already accepted it.",
                order.client_order_id,
            )
            return PlaceAck(broker_order_id=None, fills=())
        self._order_no_cache[order.client_order_id] = (order_no, order.market)
        self._awaiting_order_no.discard(order.client_order_id)
        # Fills arrive via the reconciliation loop in v1 (no per-fill ack data).
        return PlaceAck(broker_order_id=order_no, fills=())

    # ----------------------------------------------------------------- cancel
    async def cancel(self, client_order_id: str) -> CancelAck:
        resolved = self._order_no_cache.get(client_order_id)
        if resolved is None and self._resolve_order is not None:
            resolved = await self._resolve_order(client_order_id)
        if resolved is None:
            if client_order_id in self._awaiting_order_no:
                # A materially different situation from "unknown order", and the caller must
                # be able to tell them apart: this one IS live at the venue. Reporting both
                # as "no mapping" is the same class of defect as the ack that caused it.
                return CancelAck(
                    ok=False,
                    reason=(
                        "order is LIVE at the venue but its broker_order_id has not been "
                        "reconciled yet (the place-ack carried no orderNo) — retry after the "
                        "next reconcile pass; do NOT resubmit"
                    ),
                )
            return CancelAck(ok=False, reason="no broker_order_id mapping for client_order_id")
        order_no, market = resolved
        payload = mapping.to_cancel_payload(order_no)
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
        """Venue-truth positions for ``account``, from ``POST /va/portfolio``.

        ✅ **Implemented 2026-08-28 against the first POPULATED capture** — every field
        parsed here was OBSERVED on the wire, none is inferred. Full record and the
        verbatim payloads: umbrella ``docs/reference/liberator-account-reads.md`` §2.2.

        This replaced a ``LiberatorPositionsUncaptured`` (501) that stood while the
        element schema was unknown. The refusal was correct at the time and is the
        reason there is no invented parse here now: the ten field names recoverable
        from the venue's web client turned out to be a **lower bound** — the venue
        sends **17** on a TFEX row — and one of the ten (``optVal``) does not exist at
        all. Implementing against them would have shipped a schema known to be wrong.

        🔴 **Refusal and emptiness are different, and only ``errMsg`` separates them.**
        The bridge returns ``success: true`` and ``errorCode: 0`` for an account the
        venue REFUSED, identical to an authorized-but-flat one ([[TK-0420]], confirmed
        against a populated response). ``_venue_result`` reads ``errMsg`` and raises, so
        an unreadable account can never render as ``[]``.

        Reads ``result.stock[]`` and ignores ``result.list[]`` — the latter is a plain
        list of symbol-name strings, carrying no independent position data.
        """
        market = _position_market(account)
        body = await self._transport.get_json(f"{_PORTFOLIO_PATH}/{account}")
        result = _venue_result(body, what="portfolio")
        rows = result.get("stock")
        if rows is None:
            raise LiberatorTransportError(
                "liberator portfolio: raw_response.result carried no stock array"
            )
        if not isinstance(rows, list):
            raise LiberatorTransportError(
                f"liberator portfolio: result.stock is {type(rows).__name__}, not a list"
            )
        # 🔴 A ZERO-QUANTITY ROW IS NOT A HOLDING, and it does not look empty — §2.2a④.
        # The captured `SAWADU26` came back `actualVol: 0, avg: 0, amount: 0` while still
        # carrying `sideShow: "Long"` and `positionShow: "Open"`. **Row count is not
        # position count**, and neither `sideShow` nor `positionShow` may decide whether a
        # position exists — a caller trusting the row reports a long that is not there.
        #
        # ⚠️ Filtered HERE rather than left to callers: this route is the venue-truth
        # surface, and a phantom row with a populated side is precisely the kind of
        # confident-looking wrong answer it exists to eliminate.
        parsed = (_position(account, market, raw) for raw in rows if isinstance(raw, dict))
        return [p for p in parsed if p.net_qty != 0]

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

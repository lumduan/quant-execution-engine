"""The order surface (frozen-minimal per the ROADMAP).

Public mode answers only health, capabilities, and reads (E3); order
submission, cancel, amend, and the kill-switch admin are owner-mode. The amend
HTTP route (``PATCH /orders/{cid}``) lands in Phase 4 — the promise the Phase-3
``router.amend`` docstring made is now kept. The ``/admin/*`` routes are
engine-direct only — the gateway never proxies them. The Phase-5 streaming
read surface (order-book snapshots/SSE + ``GET /orders/stream``) lives in
``api/streams.py``.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from src.quant_execution_engine import __version__
from src.quant_execution_engine.adapters.liberator.runtime import get_liberator_adapter
from src.quant_execution_engine.adapters.streaming_pro.runtime import get_streaming_pro_adapter
from src.quant_execution_engine.api.deps import (
    get_operator_id,
    get_router_dep,
    get_settings_dep,
    get_strategy_id,
    require_api_key,
    require_owner_mode,
    require_read_or_full_key,
)
from src.quant_execution_engine.api.schemas import (
    AmendOrderRequest,
    BrokerRuntimeHealth,
    CapabilitiesResponse,
    HealthResponse,
    KillSwitchEngageResponse,
    KillSwitchStateResponse,
    OrderBookHealth,
)
from src.quant_execution_engine.cache.errors import CacheError
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.capabilities import CAPABILITY_MATRIX
from src.quant_execution_engine.contracts.enums import Broker, to_public_status
from src.quant_execution_engine.contracts.errors import KillSwitchNotEngagedError
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core.router import OrderRouter
from src.quant_execution_engine.order_book.runtime import (
    get_order_book_router,
    get_order_book_service,
)

logger = logging.getLogger(__name__)

router = APIRouter()

RouterDep = Annotated[OrderRouter, Depends(get_router_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def _broker_runtime_health() -> dict[str, BrokerRuntimeHealth] | None:
    """Breaker/session state per configured broker (None when broker-free).

    The dict is non-None when a broker runtime exists; each configured broker
    contributes its own ``{breaker_state, session_healthy}`` entry.
    """
    brokers: dict[str, BrokerRuntimeHealth] = {}
    liberator = get_liberator_adapter()
    if liberator is not None:
        brokers["liberator"] = BrokerRuntimeHealth(
            breaker_state=liberator.breaker.state.value,
            session_healthy=liberator.last_heartbeat_ok,
        )
    streaming_pro = get_streaming_pro_adapter()
    if streaming_pro is not None:
        brokers["streaming_pro"] = BrokerRuntimeHealth(
            breaker_state=streaming_pro.breaker.state.value,
            session_healthy=streaming_pro.last_heartbeat_ok,
        )
    return brokers or None


def _order_book_health() -> OrderBookHealth | None:
    """Order-book runtime state for /health (None when the service is off)."""
    service = get_order_book_service()
    ob_router = get_order_book_router()
    if service is None or ob_router is None:
        return None
    return OrderBookHealth(
        active_provider=ob_router.active.value,
        providers=[provider.name.value for provider in ob_router.providers],
        cached_symbols=service.cached_symbol_count,
        subscribers=service.subscriber_count,
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: SettingsDep) -> HealthResponse:
    """Mapped to host ``:8400`` (container ``:8000``) in compose."""
    return HealthResponse(
        version=__version__,
        stage=settings.stage,
        public_mode=settings.public_mode,
        brokers=_broker_runtime_health(),
        order_book=_order_book_health(),
    )


def _routable_now(broker: Broker) -> bool:
    """Can THIS deployment actually route ``broker`` right now?

    🔴 The reason this exists: ``adapter_installed`` used to be served straight from
    :data:`CAPABILITY_MATRIX`, where it is a **hardcoded ``True``** — a build-time constant
    wearing a deployment-fact name. It meant *"an adapter class exists in this codebase"* and
    was read, reasonably, as *"this node can route it"*.

    On the AWS node that gap was live and load-bearing: ``/capabilities`` reported
    ``liberator adapter_installed=True`` while the node held **no Liberator credential**, so a
    ``micro_live`` order would have been ``StageRejected``. ``session:cash-carry`` hit it while
    planning a gate — and `docs/broker-commands.md` §6 tells strategy authors to *"query
    /capabilities, don't hardcode"*, so the documented advice led straight into it.

    ⚠️ Note what a ``False`` here does and does not mean. It means **not routable on this node
    as currently configured** — which at ``sim``/``paper`` includes the real brokers, because no
    real runtime is constructed below ``paper`` + owner mode + credentials. It does **not** mean
    the adapter is missing from the build. The response carries ``stage`` alongside, which is
    the context that disambiguates it.
    """
    if broker is Broker.SIM:
        return True  # SimAdapter is always constructed; it needs no credential
    if broker is Broker.LIBERATOR:
        return get_liberator_adapter() is not None
    if broker is Broker.STREAMING_PRO:
        return get_streaming_pro_adapter() is not None
    return False


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    dependencies=[Depends(require_api_key)],
    summary="Declared per-(broker, market) capability sets",
)
async def capabilities(settings: SettingsDep) -> CapabilitiesResponse:
    """The matrix (D7) — rows are static; ``adapter_installed`` is RUNTIME-computed.

    The order-type/tif/position-effect cells are the frozen contract the router enforces and
    are served unchanged. Only ``adapter_installed`` is recomputed per request, because it is
    the one field that asserts something about *this deployment* rather than about the
    contract — see :func:`_routable_now`.
    """
    return CapabilitiesResponse(
        stage=settings.stage,
        capabilities=tuple(
            row.model_copy(update={"adapter_installed": _routable_now(row.broker)})
            for row in CAPABILITY_MATRIX
        ),
        brokers=_broker_runtime_health(),
    )


@router.post(
    "/orders",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    status_code=status.HTTP_201_CREATED,
    summary="Submit a NormalizedOrder (idempotent on client_order_id)",
)
async def submit_order(
    order: NormalizedOrder,
    order_router: RouterDep,
    strategy_id: Annotated[str | None, Depends(get_strategy_id)],
) -> JSONResponse:
    """201 on first accept; 200 with the prior result on an idempotent resend.

    The optional ``X-Strategy-Id`` header (D16) is stamped onto the order and
    echoed on the order-update stream; absent it, behavior is unchanged.
    """
    outcome = await order_router.submit(order, strategy_id=strategy_id)
    # ``resolution`` (TK-0423) answers what the order state cannot: did we READ the
    # venue, or are we guessing? It is per-submit knowledge, not persisted state, so
    # it is merged here rather than carried on the frozen result contract — and
    # `GET /orders/{cid}` deliberately does NOT carry it (a later read is not evidence
    # about what we knew at submit time).
    #
    # 🔴 `pending` (venue read, order working) and `unknown` (venue NOT read, order may
    # be LIVE) must never be collapsed by a caller. Only `unknown` means the handle was
    # not recovered — and a resubmit on it double-fills.
    content = outcome.result.wire_dump()
    content["resolution"] = outcome.resolution.value
    return JSONResponse(
        status_code=status.HTTP_200_OK if outcome.duplicate else status.HTTP_201_CREATED,
        content=content,
    )


@router.get(
    "/orders/{client_order_id}",
    dependencies=[Depends(require_api_key)],
    summary="Read one order's normalized state",
)
async def get_order(client_order_id: str, order_router: RouterDep) -> JSONResponse:
    result = await order_router.get(client_order_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.wire_dump())


@router.get(
    "/accounts/{account}",
    dependencies=[Depends(require_read_or_full_key), Depends(require_owner_mode)],
    summary="Normalized account balance / buying power (venue truth)",
)
async def get_account(
    account: str,
    broker: Broker,
    order_router: RouterDep,
) -> JSONResponse:
    """One shape for every broker, so a strategy never learns two dialects.

    ``broker`` is a required query parameter: an account number does not name a
    broker, and guessing one would be the same class of invention that produced
    [[TK-0396]].

    🔴 **Every optional field means "this broker did not report it", NEVER zero.**
    ``None`` serialises as ``null`` and must not be re-collapsed to ``0`` by a
    caller — that collapse IS the bug this endpoint's adapter was fixed for, where
    a fabricated ``0`` was returned for accounts holding real five-figure balances.

    Coverage is deliberately asymmetric (the venues are): the margin block is
    DERIVATIVE-only and is *forbidden* on a cash account, not merely absent.
    """
    info = await order_router.get_account(broker, account)
    return JSONResponse(status_code=status.HTTP_200_OK, content=info.model_dump(mode="json"))


@router.get(
    "/accounts/{account}/positions",
    dependencies=[Depends(require_read_or_full_key), Depends(require_owner_mode)],
    summary="VENUE-TRUTH holdings for one account",
)
async def get_positions(
    account: str,
    broker: Broker,
    order_router: RouterDep,
) -> JSONResponse:
    """What the venue says this account HOLDS — not what our store thinks it filled.

    🔴 **An empty list means "this account holds nothing", and it can only mean that
    because every path that cannot answer RAISES instead.** That is the entire design
    here and it is why the route was withheld until it was true: a positions endpoint
    that returns ``[]`` when it failed to read is worse than no endpoint, because
    "flat" is a plausible answer that a caller will act on.

    ⚠️ **Coverage is asymmetric and the asymmetry is deliberate, not a gap being hidden:**

    * ``liberator`` — SET **and** TFEX parse; schema observed 2026-08-28.
    * ``streaming_pro`` — SET parses. A **TFEX** account that genuinely holds something
      answers **501** ``streaming_pro_positions_uncaptured``: that element schema has
      never been observed non-empty, and inventing field names is the defect this whole
      endpoint family was rebuilt to remove. A flat TFEX account returns ``[]`` honestly,
      because the derivatives front refuses accounts it does not hold — so reaching the
      empty case proves the account was read, not skipped.

    ``side`` is ``Side | None``, and ``None`` means *the venue did not distinguish* —
    never *flat*, never *long*. SET equities cannot be short and neither venue sends a
    side for them.
    """
    positions = await order_router.get_positions(broker, account)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"positions": [p.model_dump(mode="json") for p in positions]},
    )


@router.get(
    "/accounts/{account}/open-orders",
    dependencies=[Depends(require_read_or_full_key), Depends(require_owner_mode)],
    summary="VENUE-TRUTH resting orders for one account (not the durable store)",
)
async def get_open_orders(
    account: str,
    broker: Broker,
    order_router: RouterDep,
) -> JSONResponse:
    """What is LIVE at the venue right now — a different question from order history.

    ⚠️ Named ``open-orders`` rather than ``orders`` on purpose. This is RESTING-only,
    it is the venue's view rather than ours, and for Liberator the venue list is
    **today-only**. It cannot answer *"what happened to my order"*, and it carries no
    ``client_order_id`` — the venue echoes nothing the client sent, which is why the
    reconciler has to fuzzy-match at all.

    For history, joinable by your own ``client_order_id`` and spanning every stage,
    read the durable store via ``GET /orders/{client_order_id}``.
    """
    orders = await order_router.get_open_orders(broker, account)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"orders": [o.model_dump(mode="json") for o in orders]},
    )


# ── Order history (GH #398, operator ruling 2026-09-29) ───────────────────────────────────
# Each response says what it CANNOT contain, in the payload itself, so a consumer cannot mistake
# either route for complete history. The two sources are different questions, not two views of
# one answer: the store is complete for what THIS ENGINE routed; the venue list covers every
# origin, but only for the span the venue keeps.

_STORE_COVERAGE: dict[str, object] = {
    "source": "engine_store",
    "contains": "orders submitted THROUGH THIS ENGINE on THIS node, with their fills",
    "cannot_contain": [
        "orders placed outside this engine — by hand in the broker's app, or by any other "
        "application",
        "orders routed by the other node's engine",
    ],
}

# Evidence level stated per broker, because it differs: Liberator's day scope is OBSERVED,
# Streaming Pro's has never been measured, and treating the second like the first would be a guess.
_VENUE_SCOPE: dict[Broker, dict[str, object]] = {
    Broker.LIBERATOR: {
        "day_scope": "today_only",
        "day_scope_evidence": "OBSERVED — the venue's order list is scoped to the current day; "
        "it keeps no order history (umbrella docs/reference/liberator-account-reads.md §9.3)",
        "cannot_contain": ["any order from a previous trading day"],
    },
    Broker.STREAMING_PRO: {
        "day_scope": "not_established",
        "day_scope_evidence": "NOT MEASURED — whether this venue's order list is day-scoped has "
        "not been established (TK-0459). Treat it as today only until it is.",
        "cannot_contain": ["possibly any order from a previous trading day"],
    },
}
_SIM_SCOPE: dict[str, object] = {
    "day_scope": "simulated",
    "day_scope_evidence": "sim has no venue; this list is always empty",
    "cannot_contain": ["anything real"],
}


def _aware(value: datetime | None, name: str, default: datetime) -> datetime:
    """A naive timestamp is refused, never assumed UTC — a wrong zone raises nothing later."""
    if value is None:
        return default
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{name!r} must carry a timezone offset (e.g. 2026-09-29T00:00:00+07:00)",
        )
    return value


def _encode_cursor(created_at: datetime, client_order_id: str) -> str:
    """Opaque and URL-SAFE: a raw ISO offset carries ``+``, which a query decodes as a space."""
    raw = f"{created_at.isoformat()}|{client_order_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        ts, sep, cid = raw.partition("|")
        parsed: datetime | None = datetime.fromisoformat(ts)
    except (ValueError, UnicodeDecodeError):
        parsed, sep, cid = None, "", ""
    if not sep or not cid or parsed is None or parsed.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="malformed cursor — pass back the next_cursor value unchanged",
        )
    return parsed, cid


@router.get(
    "/accounts/{account}/orders",
    dependencies=[Depends(require_read_or_full_key), Depends(require_owner_mode)],
    summary="Order HISTORY for one account from the engine's store — engine-routed orders only",
)
async def get_order_history(
    account: str,
    broker: Broker,
    order_router: RouterDep,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> JSONResponse:
    """Every order THIS ENGINE routed for the account, newest first, with fills. Paginated.

    ⚠️ Complete only for what went through this engine. An order placed by hand is NEVER here —
    ``core/router.py``'s submit path is the store's only insert. ``coverage`` says so in the
    response, and ``complete_since`` gives the store's first order on this node.
    """
    lo = _aware(since, "since", datetime(1970, 1, 1, tzinfo=UTC))
    hi = _aware(until, "until", datetime.now(UTC))
    rows, fills, first_at = await order_router.get_order_history(
        broker, account, since=lo, until=hi, before=_parse_cursor(cursor), limit=limit + 1
    )
    page, more = rows[:limit], len(rows) > limit
    orders = []
    for row in page:
        body = row.model_dump(mode="json")
        body["public_status"] = to_public_status(row.status, row.filled_qty).value
        body["fills"] = [
            f.model_dump(mode="json", include={"broker_fill_id", "price", "quantity", "exec_ts"})
            for f in fills.get(row.client_order_id, [])
        ]
        orders.append(body)
    last = page[-1] if (more and page) else None
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "account": account,
            "broker": broker.value,
            "coverage": {
                **_STORE_COVERAGE,
                "complete_since": first_at.isoformat() if first_at else None,
                "window": {"since": lo.isoformat(), "until": hi.isoformat()},
            },
            "orders": orders,
            "next_cursor": _encode_cursor(last.created_at, last.client_order_id) if last else None,
        },
    )


@router.get(
    "/accounts/{account}/venue-orders",
    dependencies=[Depends(require_read_or_full_key), Depends(require_owner_mode)],
    summary="The venue's OWN order list for one account — every state, every origin",
)
async def get_venue_orders(
    account: str,
    broker: Broker,
    order_router: RouterDep,
) -> JSONResponse:
    """Every row the broker lists for the account, including orders placed by hand.

    The venue lists by ACCOUNT, not by client — so this is the only engine route that can show an
    order the engine did not send. Unlike ``/open-orders`` it keeps filled and cancelled rows. Its
    day scope differs by broker and is stated, with its evidence level, in ``coverage``.
    """
    views = await order_router.get_venue_orders(broker, account)
    scope = _VENUE_SCOPE.get(broker, _SIM_SCOPE)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "account": account,
            "broker": broker.value,
            "coverage": {
                "source": "venue_order_list",
                "contains": "every order the broker lists for this account, in ANY state and "
                "from ANY origin — the list is per account, not per client, so orders placed "
                "by hand are expected here too (inferred; not yet observed)",
                **scope,
            },
            "retrieved_at": datetime.now(UTC).isoformat(),
            "orders": [v.model_dump(mode="json") for v in views],
        },
    )


@router.delete(
    "/orders/{client_order_id}",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Cancel a resting order (frozen edges only)",
)
async def cancel_order(client_order_id: str, order_router: RouterDep) -> JSONResponse:
    """Deliberately NOT blocked by the kill-switch — cancels reduce risk."""
    result = await order_router.cancel(client_order_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.wire_dump())


@router.patch(
    "/orders/{client_order_id}",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Amend a resting order's price/quantity",
)
async def amend_order(
    client_order_id: str, body: AmendOrderRequest, order_router: RouterDep
) -> JSONResponse:
    """Amend in place (native, same cid) or cancel+replace (returns the new cid).

    The router branches on the order's declared amend semantics: a native broker
    (sim) returns the SAME ``client_order_id`` with the updated price/qty; a
    cancel_replace broker (Liberator, Streaming Pro) returns the REPLACEMENT cid
    — the honest answer, since a new order object was created. The kill-switch is
    enforced
    INSIDE ``router.amend`` (amends can increase exposure), not duplicated here.
    Typed errors flow through the envelope handlers: 404 order_not_found, 409
    amend_rejected / illegal_transition, 403 public-mode / kill-switch, 422
    risk / capability, 503 broker_circuit_open.
    """
    outcome = await order_router.amend(
        client_order_id,
        new_client_order_id=body.new_client_order_id,
        new_price=body.new_price,
        new_qty=body.new_qty,
    )
    return JSONResponse(status_code=status.HTTP_200_OK, content=outcome.result.wire_dump())


@router.get(
    "/admin/kill-switch",
    response_model=KillSwitchStateResponse,
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Kill-switch state",
)
async def kill_switch_state(order_router: RouterDep) -> KillSwitchStateResponse:
    engaged, source = await order_router.kill_switch.status()
    return KillSwitchStateResponse(engaged=engaged, source=source)


@router.post(
    "/admin/kill-switch/engage",
    response_model=KillSwitchEngageResponse,
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Trip the kill-switch: reject all new submits + mass-cancel open orders",
)
async def kill_switch_engage(
    order_router: RouterDep,
    operator: Annotated[str, Depends(get_operator_id)],
) -> KillSwitchEngageResponse:
    """Idempotent trip: a second engage returns ``already_engaged=true`` and runs
    NO second mass-cancel. The first engage trips the switch, sweeps all open
    orders, and emits a structured ``kill_switch.engaged`` audit log (operator +
    counts; never any secret).
    """
    engaged, _ = await order_router.kill_switch.status()
    if engaged:
        return KillSwitchEngageResponse(
            engaged=True, already_engaged=True, cancelled_count=0, cancelled=[], failed=[]
        )
    try:
        await order_router.kill_switch.engage()
    except CacheError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    cancelled, failed = await order_router.mass_cancel()
    logger.info(
        "%s",
        json.dumps(
            {
                "event": "kill_switch.engaged",
                "operator": operator,
                "cancelled_count": len(cancelled),
                "failed_count": len(failed),
            }
        ),
    )
    return KillSwitchEngageResponse(
        engaged=True,
        already_engaged=False,
        cancelled_count=len(cancelled),
        cancelled=cancelled,
        failed=failed,
    )


@router.post(
    "/admin/kill-switch/disengage",
    response_model=KillSwitchStateResponse,
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
    summary="Clear the runtime kill-switch trip (the env flag always wins)",
)
async def kill_switch_disengage(
    order_router: RouterDep,
    operator: Annotated[str, Depends(get_operator_id)],
) -> KillSwitchStateResponse:
    """Disengage the runtime trip. 409 ``kill_switch_not_engaged`` when the switch
    is already clear (distinct from the env-pinned 409); env-pinned disengage
    still raises ``kill_switch_env_pinned``. Emits a structured
    ``kill_switch.disengaged`` audit log with the operator identity.
    """
    engaged, _ = await order_router.kill_switch.status()
    if not engaged:
        # Status-first: a clear switch is a 409 here. This also means the
        # redis-unavailable case is already a 409 (status() with no redis reports
        # not-engaged), so disengage() — which only raises CacheError when redis
        # is absent — has no reachable CacheError path from this route.
        raise KillSwitchNotEngagedError("kill switch is not currently engaged")
    # Env-pinned disengage still raises KillSwitchPinnedError (409 env_pinned).
    await order_router.kill_switch.disengage()
    logger.info("%s", json.dumps({"event": "kill_switch.disengaged", "operator": operator}))
    engaged, source = await order_router.kill_switch.status()
    return KillSwitchStateResponse(engaged=engaged, source=source)

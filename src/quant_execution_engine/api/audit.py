"""Structured audit read + NDJSON export over append-only ``order_events`` (E1/E2).

Two owner-mode reads synthesized from the EXISTING ``execution.order_events``
columns — NO ``quant-infra-db`` schema change (Design Decision §3). The store has
``from_status``/``to_status``/``event`` (JSONB) only: this module derives the
audit-friendly ``seq`` (per-order ordinal), ``event_type`` (a pure transition
mapping), ``broker_order_id`` + ``metadata`` (from the ``event`` JSONB), and
``occurred_at`` (``created_at`` as UTC ISO-8601). Both routes are READS — no DB
write — and owner-mode only (403 ``problem+json`` in public mode), mirroring the
``/admin/*`` precedent in ``routes.py``.

Route ordering: the literal ``/admin/audit/export`` is declared BEFORE the
``/admin/orders/{client_order_id}/audit`` path-param route, and this router is
included after ``streams`` and before nothing that shadows it — neither collides
with the core ``/orders/{client_order_id}`` surface (the ``/admin`` prefix is
disjoint).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from src.quant_execution_engine.api.deps import (
    get_pool_dep,
    require_api_key,
    require_owner_mode,
)
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.contracts.errors import OrderNotFound
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.models import OrderEventRow

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_api_key), Depends(require_owner_mode)],
)

_NDJSON_MEDIA_TYPE = "application/x-ndjson"


def event_type_for(from_status: OrderState | None, to_status: OrderState) -> str:
    """Pure ``(from_status, to_status)`` → audit ``event_type`` label (total).

    Derived, never stored (Design Decision §3). The mapping keys off ``to_status``
    first (the edge's destination is the event's meaning) with a couple of
    source-qualified cases; the ``PENDING_CANCEL`` kill-switch sweep surfaces as
    ``cancel_request`` then ``cancel`` (Design Decision §6). A safe default keeps
    the function total against any future edge.
    """
    if from_status is None:
        return "create"  # the NULL -> PENDING_NEW birth row
    if to_status is OrderState.NEW:
        # PENDING_REPLACE -> NEW is the native-amend resolution; PENDING_NEW -> NEW
        # is the venue ack. Both restore the resting state; distinguish by source.
        return "replace" if from_status is OrderState.PENDING_REPLACE else "ack"
    if to_status in (OrderState.PARTIALLY_FILLED, OrderState.FILLED):
        return "fill"
    if to_status is OrderState.PENDING_CANCEL:
        return "cancel_request"
    if to_status is OrderState.CANCELLED:
        return "cancel"
    if to_status is OrderState.PENDING_REPLACE:
        return "replace_request"
    if to_status is OrderState.REJECTED:
        return "reject"
    if to_status is OrderState.EXPIRED:
        return "expire"
    if to_status is OrderState.PENDING_NEW:  # pragma: no cover - only ever the birth row
        return "create"
    return "transition"  # pragma: no cover - total-mapping safety net


def _to_utc_iso(ts: datetime) -> str:
    """``created_at`` as a UTC ISO-8601 string (store UTC, surface UTC)."""
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _synthesize_event(seq: int, row: OrderEventRow) -> dict[str, Any]:
    """One synthesized audit event (Design Decision §3 — derived, no schema change).

    ``seq`` = 1-based per-order ordinal; ``broker_order_id`` = ``event->>...``;
    ``metadata`` = the full opaque ``event`` JSONB; ``event_type`` = the pure
    transition mapping; ``occurred_at`` = ``created_at`` as UTC ISO-8601.
    """
    metadata = row.event or {}
    broker_order_id = metadata.get("broker_order_id") if isinstance(metadata, dict) else None
    return {
        "seq": seq,
        "from_status": row.from_status.value if row.from_status is not None else None,
        "to_status": row.to_status.value,
        "broker_order_id": broker_order_id,
        "event_type": event_type_for(row.from_status, row.to_status),
        "occurred_at": _to_utc_iso(row.created_at),
        "metadata": metadata,
    }


@router.get(
    "/audit/export",
    summary="Stream every order_events row as NDJSON (owner-mode; date/strategy filter)",
)
async def audit_export(
    pool: Annotated[asyncpg.Pool, Depends(get_pool_dep)],
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    strategy_id: str | None = None,
) -> StreamingResponse:
    """NDJSON export of ``order_events`` (joined to ``orders`` for ``strategy_id``).

    One JSON object per line, streamed as fetched via a server-side cursor (E2 —
    never buffered). Filters: ``from_ts`` inclusive, ``to_ts`` exclusive,
    ``strategy_id`` exact. ``Content-Disposition`` names the range (``all`` when a
    bound is absent). Owner-mode only (router-level deps); a pure read.
    """

    async def _lines() -> AsyncGenerator[bytes, None]:
        async for record in repositories.stream_order_events(
            pool, from_ts=from_ts, to_ts=to_ts, strategy_id=strategy_id
        ):
            yield (json.dumps(record) + "\n").encode("utf-8")

    lo = from_ts.date().isoformat() if from_ts is not None else "all"
    hi = to_ts.date().isoformat() if to_ts is not None else "all"
    headers = {"Content-Disposition": f'attachment; filename="audit_{lo}_{hi}.ndjson"'}
    return StreamingResponse(_lines(), media_type=_NDJSON_MEDIA_TYPE, headers=headers)


@router.get(
    "/orders/{client_order_id}/audit",
    summary="Full synthesized audit trail for one order (owner-mode)",
)
async def order_audit(
    client_order_id: str,
    pool: Annotated[asyncpg.Pool, Depends(get_pool_dep)],
) -> JSONResponse:
    """Synthesized per-order audit trail (Design Decision §3).

    404 ``order_not_found`` when the cid is absent from ``execution.orders``;
    otherwise the order header (``broker``/``symbol``) plus its ordered events,
    each carrying the derived ``seq``/``event_type``/``occurred_at`` and the opaque
    ``metadata``. Read-only — no DB write.
    """
    header = await repositories.fetch_order(pool, client_order_id)
    if header is None:
        raise OrderNotFound("unknown client_order_id", client_order_id=client_order_id)
    rows = await repositories.fetch_order_events(pool, client_order_id)
    events = [_synthesize_event(i, row) for i, row in enumerate(rows, start=1)]
    return JSONResponse(
        content={
            "client_order_id": client_order_id,
            "broker": header.broker.value,
            "symbol": header.symbol,
            "events": events,
        }
    )

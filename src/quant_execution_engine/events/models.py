"""Order-update event schema (Phase 5, ADR D14/D15 — Streaming Event Schema).

Frozen Pydantic — these cross the SSE boundary serialized (umbrella hard rule 3).
``Decimal``-as-string on the wire; tz-aware UTC ``ts``/``exec_ts``. The
``engine_state`` is the internal 9-value truth; ``status`` is the frozen public
6-value enum, derived via the EXISTING :func:`to_public_status` mapping (Phase 2
E8 — there is exactly one engine-state→public-status mapping in this service).

The ``event:`` field on the SSE wire is exactly the ``engine_state`` string — a
strict subset of the frozen state machine — plus the two advisory frames
(``gap``, ``resync_required``) the route emits directly; no invented states.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.quant_execution_engine.contracts.enums import (
    OrderState,
    PublicOrderStatus,
    WireDecimal,
)


class FillEvent(BaseModel):
    """One fill carried on a ``PARTIALLY_FILLED``/``FILLED`` order-update event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker_fill_id: str
    price: WireDecimal
    quantity: int
    exec_ts: datetime


class OrderUpdateEvent(BaseModel):
    """A single order state transition mirrored from the durable store.

    ``quantity``/``price`` are populated on replace (native-amend) events so a
    subscriber sees the amended values without a re-read; elsewhere they stay
    ``None``. ``fill`` is present only on fill events. No account number, PIN,
    token, or raw broker payload EVER appears here (hard rule: no secrets on the
    wire) — ``broker_order_id`` is a venue id and is safe.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    client_order_id: str
    strategy_id: str | None
    engine_state: OrderState
    status: PublicOrderStatus
    broker_order_id: str | None = None
    price: WireDecimal | None = None
    quantity: int | None = None
    fill: FillEvent | None = None
    ts: datetime

    def wire_dump(self) -> dict[str, Any]:
        """Public JSON shape: ``Decimal``-as-string, ``datetime`` ISO-UTC."""
        return self.model_dump(mode="json")


class GapMarker(BaseModel):
    """Queue-overflow advisory: ``dropped`` events were discarded for a lagging
    subscriber. Surfaced once, ahead of the next delivered event, as an
    ``event: gap`` frame so the consumer knows to re-read if it cares.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dropped: int


def derive_status(engine_state: OrderState) -> PublicOrderStatus:
    """Public status for an event, via the one existing mapping (Phase 2 E8).

    Reuses :func:`to_public_status` (the sole engine-state→public-status mapping
    in this service) with a zero fill watermark: the per-event hook does not
    re-aggregate fills, so the transient pending states surface as ``NEW``
    (``PENDING_NEW``/``PENDING_CANCEL``/``PENDING_REPLACE``) while every terminal
    and fill state maps unconditionally. A subscriber reads the truthful
    ``engine_state`` for the exact internal state.
    """
    from src.quant_execution_engine.contracts.enums import to_public_status

    return to_public_status(engine_state, 0)

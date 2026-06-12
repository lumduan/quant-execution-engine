"""Order-update event schema + in-process ``EventHub`` (Phase 5, D14/D15).

The streaming-out side of the execution engine: every frozen state transition
that lands durably in the ``execution`` store is mirrored, post-success, as an
:class:`OrderUpdateEvent` on a process-local :class:`EventHub` ring buffer that
``GET /orders/stream`` fans out over SSE. The stream is **advisory; the durable
store is truth** — the streaming analogue of the §A at-least-once + reconcile
doctrine. Publishing NEVER raises into the order path (D15).
"""

from __future__ import annotations

from src.quant_execution_engine.events.hub import (
    EventHub,
    Subscription,
    create_event_hub,
    get_event_hub,
    reset_event_hub,
)
from src.quant_execution_engine.events.models import (
    FillEvent,
    GapMarker,
    OrderUpdateEvent,
)

__all__ = [
    "EventHub",
    "FillEvent",
    "GapMarker",
    "OrderUpdateEvent",
    "Subscription",
    "create_event_hub",
    "get_event_hub",
    "reset_event_hub",
]

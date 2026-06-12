"""``EventHub`` — the in-process order-update fan-out (Phase 5, D15).

A single process-local hub: the repository write functions call :meth:`publish`
post-success, and ``GET /orders/stream`` subscribers drain bounded per-subscriber
queues. Design invariants (D15 — the stream can never block, slow, or fail the
order path):

* **Publish never raises into the caller.** The fan-out is wrapped in one
  sanctioned broad ``except`` that logs loudly (the order path must survive any
  stream-plumbing fault).
* **Monotonic ``seq``** from ``itertools.count(1)`` doubles as the SSE event id;
  a ring buffer (``collections.deque(maxlen=…)``) replays the recent window on
  reconnect, else the route emits ``resync_required``.
* **Back-pressure is lossy-tolerant.** A full subscriber queue drops its oldest
  item and records a ``GapMarker`` so the next successful read surfaces the loss
  exactly once — never a block.

Single-process only (§H): one uvicorn worker in compose. Multi-worker fan-out
(Redis pub/sub) is deferred.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.events.models import (
    FillEvent,
    GapMarker,
    OrderUpdateEvent,
    derive_status,
)

logger = logging.getLogger(__name__)

# LRU cap for the cid→strategy_id attribution map. Bounded so a long-lived
# engine never grows it without limit; eviction only loses the in-memory
# attribution hint — the durable column + the stream's DB-seeded set still
# attribute the order (D16).
_STRATEGY_MAP_CAP = 4096

_Item = OrderUpdateEvent | GapMarker


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Subscription:
    """A per-subscriber bounded queue plus its overflow gap accounting.

    The queue carries ``OrderUpdateEvent | GapMarker``; a ``GapMarker`` is a
    placeholder sentinel whose authoritative ``dropped`` count is resolved by the
    consumer via :meth:`take_dropped` when it dequeues one (so a burst of drops
    after the marker was enqueued collapses to a single, final count).
    """

    __slots__ = ("dropped", "gap_pending", "queue")

    def __init__(self, maxsize: int) -> None:
        self.queue: asyncio.Queue[_Item] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.gap_pending = False

    def offer(self, event: OrderUpdateEvent) -> None:
        """Enqueue, dropping the oldest + flagging a gap on overflow (never blocks)."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._drop_oldest()
            self.dropped += 1
            self._ensure_gap_marker()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)

    def take_dropped(self) -> int:
        """Return the accumulated drop count and reset the gap state (read-side)."""
        dropped = self.dropped
        self.dropped = 0
        self.gap_pending = False
        return dropped

    def _ensure_gap_marker(self) -> None:
        """Keep exactly one pending GapMarker queued while drops are outstanding."""
        if self.gap_pending:
            return
        self.gap_pending = True
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(GapMarker(dropped=0))

    def _drop_oldest(self) -> None:
        with contextlib.suppress(asyncio.QueueEmpty):
            dropped = self.queue.get_nowait()
            if isinstance(dropped, GapMarker):
                # The marker itself fell off — re-arm so it is re-enqueued; the
                # outstanding drop count still surfaces (take_dropped is unread).
                self.gap_pending = False


class EventHub:
    """Process-local order-update ring buffer + per-subscriber fan-out."""

    def __init__(
        self,
        *,
        ring_buffer_size: int,
        subscriber_queue_size: int,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._ring: deque[OrderUpdateEvent] = deque(maxlen=ring_buffer_size)
        self._subscriber_queue_size = subscriber_queue_size
        self._now = now
        self._seq = itertools.count(1)
        self._subscriptions: list[Subscription] = []
        self._strategy_by_cid: OrderedDict[str, str] = OrderedDict()

    # ------------------------------------------------------------- attribution

    def register_strategy(self, client_order_id: str, strategy_id: str) -> None:
        """Remember the cid→strategy attribution (bounded LRU, cap 4096)."""
        self._strategy_by_cid[client_order_id] = strategy_id
        self._strategy_by_cid.move_to_end(client_order_id)
        while len(self._strategy_by_cid) > _STRATEGY_MAP_CAP:
            self._strategy_by_cid.popitem(last=False)

    def _resolve_strategy(self, client_order_id: str, strategy_id: str | None) -> str | None:
        if strategy_id is not None:
            return strategy_id
        return self._strategy_by_cid.get(client_order_id)

    # ----------------------------------------------------------------- publish

    def publish(
        self,
        *,
        client_order_id: str,
        engine_state: OrderState,
        strategy_id: str | None = None,
        broker_order_id: str | None = None,
        price: Decimal | None = None,
        quantity: int | None = None,
        fill: FillEvent | None = None,
    ) -> OrderUpdateEvent | None:
        """Stamp + ring-append + fan out one event; NEVER raises into the caller.

        Returns the event (or ``None`` if the fan-out raised — the order path
        ignores the result either way). The whole body sits behind one sanctioned
        broad ``except`` (D15): a fault in stream plumbing must never propagate
        into a durable write that already committed.
        """
        try:
            event = OrderUpdateEvent(
                seq=next(self._seq),
                client_order_id=client_order_id,
                strategy_id=self._resolve_strategy(client_order_id, strategy_id),
                engine_state=engine_state,
                status=derive_status(engine_state),
                broker_order_id=broker_order_id,
                price=price,
                quantity=quantity,
                fill=fill,
                ts=self._now(),
            )
            self._ring.append(event)
            for subscription in self._subscriptions:
                subscription.offer(event)
            return event
        except Exception:  # noqa: BLE001
            # The ONE sanctioned broad except in the hot order path: stream
            # plumbing must never fail a committed write. Log loudly, never pass
            # silently, never re-raise.
            logger.exception("order_stream.publish_failed cid=%s", client_order_id)
            return None

    # ------------------------------------------------------------------ replay

    def replay(self, after_seq: int) -> tuple[list[OrderUpdateEvent], bool]:
        """Ring events with ``seq > after_seq``; bool = the cursor fell off.

        ``after_seq <= 0`` means "live-only" (no replay): ``([], False)``. A gap
        is reported when ``after_seq`` predates the oldest retained seq AND the
        ring is non-empty — the route then emits one ``resync_required`` frame.
        """
        if after_seq <= 0:
            return [], False
        events = [event for event in self._ring if event.seq > after_seq]
        oldest = self._ring[0].seq if self._ring else None
        gap = oldest is not None and after_seq < oldest - 1
        return events, gap

    # --------------------------------------------------------------- subscribe

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[Subscription]:
        """Yield a per-subscriber :class:`Subscription`, refcounted into fan-out.

        Mirrors ``OrderBookService.subscription``: entering registers it, the
        ``finally`` removes it (no disconnect polling — the route's ``async with``
        releases on client disconnect / cancellation). The route reads
        ``subscription.queue`` and resolves overflow drops via
        ``subscription.take_dropped()``.
        """
        subscription = Subscription(self._subscriber_queue_size)
        self._subscriptions.append(subscription)
        logger.info("order_stream.subscribers count=%d", len(self._subscriptions))
        try:
            yield subscription
        finally:
            with contextlib.suppress(ValueError):
                self._subscriptions.remove(subscription)
            logger.info("order_stream.subscribers count=%d", len(self._subscriptions))

    @property
    def subscriber_count(self) -> int:
        """Live subscriber queues (for /health / diagnostics)."""
        return len(self._subscriptions)


_hub: EventHub | None = None


def create_event_hub(settings: Settings) -> EventHub:
    """Create (or return) the process-singleton hub.

    Built in the app lifespan BEFORE the DB pool so no durable transition can
    ever beat the hub into existence (a publish before this returns simply finds
    ``get_event_hub() is None`` and no-ops — the durable store is still truth).
    """
    global _hub
    if _hub is None:
        _hub = EventHub(
            ring_buffer_size=settings.stream_ring_buffer_size,
            subscriber_queue_size=settings.stream_subscriber_queue_size,
        )
        logger.info(
            "event_hub created (ring=%d, queue=%d)",
            settings.stream_ring_buffer_size,
            settings.stream_subscriber_queue_size,
        )
    return _hub


def get_event_hub() -> EventHub | None:
    """The singleton hub, or ``None`` before the lifespan created it."""
    return _hub


def reset_event_hub() -> None:
    """Clear the singleton (tests + lifespan shutdown; no async teardown)."""
    global _hub
    _hub = None

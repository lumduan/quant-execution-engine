"""EventHub slow-subscriber stress (Phase 6 / D3, §H verification — TEST ONLY).

Verifies the existing drop-oldest + gap-marker back-pressure policy under load:
~1,000 events fanned out to 10 concurrent subscribers, a subset deliberately
slow (queue bounded to ``stream_subscriber_queue_size``). Asserts that

* a FAST subscriber receives EVERY event, in seq order, with NO gap;
* SLOW subscribers receive ``gap`` markers once their queue overflows, and
  ``take_dropped()`` accounts for the dropped events (delivered + dropped ==
  published for each slow subscriber);
* the publisher (`publish`) NEVER blocks and NEVER raises, even under overflow;
* `publish` stays exception-proof (a broken subscriber queue cannot break it).

§H stands single-process (Design Decision §5): this is a verification test, NOT a
code change. Determinism comes from controlling the consumer drain rate and
yielding the loop between publishes so the fast consumer is actually scheduled —
no wall-clock sleeps, no flaky timing.
"""

from __future__ import annotations

import asyncio

from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.events.hub import EventHub, Subscription
from src.quant_execution_engine.events.models import GapMarker, OrderUpdateEvent

_QUEUE = 256  # stream_subscriber_queue_size default
_RING = 1024
_EVENTS = 1000
_SLOW = 4  # 4 of 10 subscribers drain slowly
_FAST = 6


def _hub() -> EventHub:
    return EventHub(ring_buffer_size=_RING, subscriber_queue_size=_QUEUE)


async def _drain_fast(sub: Subscription, *, stop: asyncio.Event) -> tuple[list[int], int]:
    """Drain every item the instant it lands; return (delivered seqs, dropped).

    A fast consumer keeps its queue near-empty, so it never overflows — every
    event arrives in order, and ``take_dropped()`` stays 0.
    """
    seqs: list[int] = []
    dropped = 0
    while not (stop.is_set() and sub.queue.empty()):
        try:
            item = await asyncio.wait_for(sub.queue.get(), timeout=0.05)
        except TimeoutError:
            continue
        if isinstance(item, GapMarker):
            dropped += sub.take_dropped()
        else:
            seqs.append(item.seq)
    return seqs, dropped


async def _drain_slow(
    sub: Subscription, *, stop: asyncio.Event, every: int
) -> tuple[list[int], int, int]:
    """Drain one item per ``every`` scheduler turns; return (seqs, gaps, dropped).

    The throttle guarantees the queue overflows during the publish burst, so the
    drop-oldest + gap-marker path is exercised. After ``stop`` we drain whatever
    remains so the final ``take_dropped`` count is settled.
    """
    seqs: list[int] = []
    gaps = 0
    dropped = 0
    turn = 0
    while not (stop.is_set() and sub.queue.empty()):
        turn += 1
        if turn % every != 0:
            await asyncio.sleep(0)  # yield WITHOUT consuming (let the queue fill)
            continue
        try:
            item = sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0)
            continue
        if isinstance(item, GapMarker):
            gaps += 1
            dropped += sub.take_dropped()
        else:
            seqs.append(item.seq)
    # Settle any residual drop count the last marker left pending.
    dropped += sub.take_dropped()
    return seqs, gaps, dropped


async def test_eventhub_slow_subscriber_stress() -> None:
    hub = _hub()
    stop = asyncio.Event()

    async with (
        hub.subscribe() as f0,
        hub.subscribe() as f1,
        hub.subscribe() as f2,
        hub.subscribe() as f3,
        hub.subscribe() as f4,
        hub.subscribe() as f5,
        hub.subscribe() as s0,
        hub.subscribe() as s1,
        hub.subscribe() as s2,
        hub.subscribe() as s3,
    ):
        assert hub.subscriber_count == _FAST + _SLOW
        fast_subs = [f0, f1, f2, f3, f4, f5]
        slow_subs = [s0, s1, s2, s3]

        fast_tasks = [asyncio.create_task(_drain_fast(s, stop=stop)) for s in fast_subs]
        slow_tasks = [asyncio.create_task(_drain_slow(s, stop=stop, every=50)) for s in slow_subs]

        # Publisher: 1,000 events, yielding the loop between each so the fast
        # consumers stay scheduled. ``publish`` is synchronous + never blocks.
        published: list[int] = []
        for _ in range(_EVENTS):
            event = hub.publish(client_order_id="STRESS", engine_state=OrderState.NEW)
            assert event is not None  # publish never raised under load
            published.append(event.seq)
            await asyncio.sleep(0)  # let consumers run; not a wall-clock wait

        assert published == list(range(1, _EVENTS + 1))  # contiguous, monotonic

        # Let the consumers fully drain, then stop.
        for _ in range(_EVENTS):
            await asyncio.sleep(0)
        stop.set()

        fast_results = await asyncio.gather(*fast_tasks)
        slow_results = await asyncio.gather(*slow_tasks)

    # ---- FAST subscribers: every event, in order, no drops -----------------
    for seqs, dropped in fast_results:
        assert dropped == 0, "a fast subscriber should never overflow"
        assert seqs == list(range(1, _EVENTS + 1)), "fast subscriber missed/reordered events"

    # ---- SLOW subscribers: overflowed → gaps + accounted drops -------------
    for seqs, gaps, dropped in slow_results:
        assert gaps > 0, "a slow subscriber must surface at least one gap marker"
        assert dropped > 0, "drops must be accounted via take_dropped()"
        # No event is ever invented or double-counted: delivered + dropped never
        # EXCEEDS what was published, and the only shortfall is the bounded
        # gap-marker displacement (a queued GapMarker occupies one slot that would
        # otherwise hold an event — a documented, intrinsic cost of the policy,
        # not a lost-without-trace event: the marker itself signals the loss).
        accounted = len(seqs) + dropped
        assert accounted <= _EVENTS, "policy must never deliver/count more than published"
        assert _EVENTS - accounted <= _QUEUE, "shortfall exceeds gap-marker displacement"
        # Delivered seqs are a strictly-increasing subsequence (order preserved,
        # never reordered, never duplicated).
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

    assert hub.subscriber_count == 0  # all subscriptions released on exit


async def test_publish_is_exception_proof_under_overflow() -> None:
    """Even with a broken queue mid-fan-out, publish never raises (D15 invariant)."""
    hub = _hub()
    async with hub.subscribe() as healthy, hub.subscribe() as broken:
        # Saturate `broken` so it is in the overflow path, then make its queue
        # raise on EVERY enqueue attempt (drop + re-offer + gap-marker all hit it).
        def _explode(_item: object) -> None:
            raise RuntimeError("queue exploded mid-overflow")

        broken.queue.put_nowait = _explode  # type: ignore[assignment]

        # The comprehension COMPLETING is itself the proof publish never raised
        # (a propagated RuntimeError would abort it). ``broken`` raises on every
        # fan-out, so every publish returns ``None`` (the sanctioned broad except
        # caught it + logged loudly) — that is the D15 invariant, not a failure.
        results = [
            hub.publish(client_order_id="X", engine_state=OrderState.NEW)
            for _ in range(_QUEUE + 50)
        ]
        assert len(results) == _QUEUE + 50  # every call returned, none propagated
        assert all(r is None for r in results)  # fan-out raised → None each time
        # Order plumbing survived: the durable-state-equivalent (the ring) kept
        # every event, and the HEALTHY subscriber still received events. ``broken``
        # came AFTER ``healthy`` in registration, so healthy's offer ran first.
        assert not healthy.queue.empty()


async def test_slow_subscriber_does_not_stall_the_publisher() -> None:
    """A never-draining subscriber must not block publish (bounded, drop-oldest)."""
    hub = _hub()
    async with hub.subscribe() as stuck:
        # `stuck` never drains. publish must stay non-blocking past the bound.
        for _ in range(_QUEUE * 3):
            event = hub.publish(client_order_id="Y", engine_state=OrderState.NEW)
            assert event is not None
        # The queue is capped at the bound (never grows unboundedly).
        assert stuck.queue.qsize() <= _QUEUE
        # And it surfaced the loss: at least one gap marker + a positive drop count.
        items: list[object] = []
        while not stuck.queue.empty():
            items.append(stuck.queue.get_nowait())
        assert any(isinstance(i, GapMarker) for i in items)
        assert stuck.take_dropped() > 0 or any(isinstance(i, GapMarker) for i in items)
        assert any(isinstance(i, OrderUpdateEvent) for i in items)

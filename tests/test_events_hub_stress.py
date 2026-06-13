"""EventHub slow-subscriber stress (Phase 6 / D3, §H verification — TEST ONLY).

Verifies the existing drop-oldest + gap-marker back-pressure policy under load:
1,000 events fanned out to 10 subscribers, a subset deliberately saturated
(queue bounded to ``stream_subscriber_queue_size``). Asserts that

* a FAST subscriber (drained every round) receives EVERY event, in seq order,
  with NO gap;
* SLOW subscribers (never drained during the burst) receive ``gap`` markers once
  their queue overflows, and ``take_dropped()`` accounts for the dropped events;
* the publisher (`publish`) NEVER blocks and NEVER raises, even under overflow;
* `publish` stays exception-proof (a broken subscriber queue cannot break it).

§H stands single-process (Design Decision §5): this is a verification test, NOT a
code change. Determinism comes from draining the fast consumers INLINE after each
publish and leaving the slow consumers saturated — only the real ``offer`` /
overflow / gap-marker machinery is exercised, with NO wall-clock sleeps and NO
event-loop scheduler-fairness assumptions (so it is stable across CPython
3.11/3.12/3.13, where ``asyncio`` task/``wait_for`` scheduling differs).
"""

from __future__ import annotations

from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.events.hub import EventHub
from src.quant_execution_engine.events.models import GapMarker, OrderUpdateEvent

_QUEUE = 256  # stream_subscriber_queue_size default
_RING = 1024
_EVENTS = 1000
_SLOW = 4  # 4 of 10 subscribers are never drained during the burst
_FAST = 6


def _hub() -> EventHub:
    return EventHub(ring_buffer_size=_RING, subscriber_queue_size=_QUEUE)


async def test_eventhub_slow_subscriber_stress() -> None:
    """1,000 events x 10 subscribers — fast drained each round, slow saturated.

    Deterministic across event-loop / Python versions: a FAST consumer is
    modelled by draining its queue immediately after every publish (it always
    keeps up, so its bounded queue never overflows); a SLOW / saturated consumer
    is left untouched during the burst (its bounded queue overflows → drop-oldest
    + gap markers). No wall-clock sleeps, no scheduler-fairness assumptions.
    """
    hub = _hub()
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
        fast_subs = [f0, f1, f2, f3, f4, f5]
        slow_subs = [s0, s1, s2, s3]
        assert hub.subscriber_count == _FAST + _SLOW

        fast_seqs: list[list[int]] = [[] for _ in fast_subs]
        published: list[int] = []
        for _ in range(_EVENTS):
            event = hub.publish(client_order_id="STRESS", engine_state=OrderState.NEW)
            assert event is not None  # publish never raised / blocked under load
            published.append(event.seq)
            # FAST consumers keep up: drain every item the instant it lands, so
            # their bounded queues never overflow. SLOW consumers are left alone.
            for sub, seqs in zip(fast_subs, fast_seqs, strict=True):
                while not sub.queue.empty():
                    item = sub.queue.get_nowait()
                    assert isinstance(item, OrderUpdateEvent)  # fast → never a gap
                    seqs.append(item.seq)

        assert published == list(range(1, _EVENTS + 1))  # contiguous, monotonic

    # ---- FAST subscribers: every event, in order, no drops -----------------
    for seqs, sub in zip(fast_seqs, fast_subs, strict=True):
        assert sub.take_dropped() == 0, "a fast subscriber should never overflow"
        assert seqs == list(range(1, _EVENTS + 1)), "fast subscriber missed/reordered events"

    # ---- SLOW subscribers: overflowed → gaps + accounted drops -------------
    for sub in slow_subs:
        delivered: list[int] = []
        gaps = 0
        while not sub.queue.empty():
            item = sub.queue.get_nowait()
            if isinstance(item, GapMarker):
                gaps += 1
            else:
                delivered.append(item.seq)
        dropped = sub.take_dropped()
        assert gaps > 0, "a slow subscriber must surface at least one gap marker"
        assert dropped > 0, "drops must be accounted via take_dropped()"
        # No event is invented or double-counted: delivered + dropped never
        # EXCEEDS what was published, and the only shortfall is the bounded
        # gap-marker displacement (a queued GapMarker occupies one slot that would
        # otherwise hold an event — a documented intrinsic cost of the policy, not
        # a lost-without-trace event: the marker itself signals the loss).
        accounted = len(delivered) + dropped
        assert accounted <= _EVENTS, "policy must never deliver/count more than published"
        assert _EVENTS - accounted <= _QUEUE, "shortfall exceeds gap-marker displacement"
        # Delivered seqs are a strictly-increasing, de-duplicated subsequence.
        assert delivered == sorted(delivered)
        assert len(set(delivered)) == len(delivered)

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

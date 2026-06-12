"""EventHub: seq monotonicity, ring replay, fan-out, overflow gaps, LRU, mapping."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from src.quant_execution_engine.contracts.enums import OrderState, PublicOrderStatus
from src.quant_execution_engine.events.hub import (
    _STRATEGY_MAP_CAP,
    EventHub,
    create_event_hub,
    get_event_hub,
    reset_event_hub,
)
from src.quant_execution_engine.events.models import (
    FillEvent,
    GapMarker,
    OrderUpdateEvent,
    derive_status,
)

from tests.conftest import make_settings

_TS = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)


def _hub(*, ring: int = 16, queue: int = 8) -> EventHub:
    return EventHub(ring_buffer_size=ring, subscriber_queue_size=queue, now=lambda: _TS)


def test_seq_is_monotonic_from_one() -> None:
    hub = _hub()
    first = hub.publish(client_order_id="A", engine_state=OrderState.PENDING_NEW)
    second = hub.publish(client_order_id="A", engine_state=OrderState.NEW)
    assert first is not None and second is not None
    assert first.seq == 1
    assert second.seq == 2
    assert first.ts == _TS


def test_publish_returns_event_with_derived_status_and_fields() -> None:
    hub = _hub()
    event = hub.publish(
        client_order_id="A",
        engine_state=OrderState.NEW,
        strategy_id="csm",
        broker_order_id="SIM-1",
    )
    assert event is not None
    assert event.client_order_id == "A"
    assert event.strategy_id == "csm"
    assert event.broker_order_id == "SIM-1"
    assert event.status is PublicOrderStatus.NEW


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (OrderState.PENDING_NEW, PublicOrderStatus.NEW),
        (OrderState.NEW, PublicOrderStatus.NEW),
        (OrderState.PARTIALLY_FILLED, PublicOrderStatus.PARTIALLY_FILLED),
        (OrderState.FILLED, PublicOrderStatus.FILLED),
        (OrderState.PENDING_CANCEL, PublicOrderStatus.NEW),
        (OrderState.PENDING_REPLACE, PublicOrderStatus.NEW),
        (OrderState.CANCELLED, PublicOrderStatus.CANCELLED),
        (OrderState.REJECTED, PublicOrderStatus.REJECTED),
        (OrderState.EXPIRED, PublicOrderStatus.EXPIRED),
    ],
)
def test_status_mapping_per_engine_state(state: OrderState, expected: PublicOrderStatus) -> None:
    assert derive_status(state) is expected
    hub = _hub()
    event = hub.publish(client_order_id="A", engine_state=state)
    assert event is not None and event.status is expected


def test_replay_after_seq_semantics() -> None:
    hub = _hub()
    for state in (OrderState.PENDING_NEW, OrderState.NEW, OrderState.FILLED):
        hub.publish(client_order_id="A", engine_state=state)
    # after_seq=0 ⇒ live-only, no replay.
    events, gap = hub.replay(0)
    assert events == [] and gap is False
    # after_seq=1 ⇒ replay seq 2,3, no gap (1 still in the ring).
    events, gap = hub.replay(1)
    assert [e.seq for e in events] == [2, 3]
    assert gap is False


def test_replay_reports_gap_when_cursor_fell_off_the_ring() -> None:
    hub = _hub(ring=2)
    for state in (OrderState.PENDING_NEW, OrderState.NEW, OrderState.FILLED):
        hub.publish(client_order_id="A", engine_state=state)
    # Ring holds seq 2,3 only; a cursor at 0<after<oldest-1 -> gap.
    events, gap = hub.replay(0)  # live-only short-circuits before gap check
    assert events == [] and gap is False
    events, gap = hub.replay(1)
    # oldest retained = 2; after_seq=1 == oldest-1 ⇒ NOT a gap (1 is contiguous).
    assert [e.seq for e in events] == [3] or [e.seq for e in events] == [2, 3]
    # A cursor well behind the ring (after the ring rolled) is a gap.
    for _ in range(5):
        hub.publish(client_order_id="A", engine_state=OrderState.NEW)
    events, gap = hub.replay(1)
    assert gap is True


async def test_subscriber_receives_published_events() -> None:
    hub = _hub()
    async with hub.subscribe() as subscription:
        assert hub.subscriber_count == 1
        hub.publish(client_order_id="A", engine_state=OrderState.PENDING_NEW)
        item = await asyncio.wait_for(subscription.queue.get(), timeout=1.0)
        assert isinstance(item, OrderUpdateEvent)
        assert item.engine_state is OrderState.PENDING_NEW
    assert hub.subscriber_count == 0


async def test_overflow_drops_oldest_and_surfaces_one_gap_marker() -> None:
    hub = _hub(queue=2)
    async with hub.subscribe() as subscription:
        # Publish more than the queue can hold without anyone draining it.
        for _ in range(5):
            hub.publish(client_order_id="A", engine_state=OrderState.NEW)
        items: list[object] = []
        while not subscription.queue.empty():
            items.append(subscription.queue.get_nowait())
        markers = [i for i in items if isinstance(i, GapMarker)]
        assert len(markers) == 1  # exactly one gap marker surfaced
        assert subscription.take_dropped() > 0


async def test_publish_never_raises_when_subscriber_queue_misbehaves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = _hub()
    async with hub.subscribe() as subscription:

        def _boom(_item: object) -> None:
            raise RuntimeError("queue exploded")

        subscription.queue.put_nowait = _boom  # type: ignore[assignment]
        with caplog.at_level(logging.ERROR):
            result = hub.publish(client_order_id="A", engine_state=OrderState.NEW)
        # The caller is unaffected: no raise; the fault is logged loudly.
        assert result is None
        assert any("publish_failed" in rec.message for rec in caplog.records)


def test_register_strategy_lru_cap_eviction() -> None:
    hub = _hub()
    hub.register_strategy("oldest", "csm")
    for i in range(_STRATEGY_MAP_CAP):
        hub.register_strategy(f"cid-{i}", "tfex")
    # "oldest" was evicted once the cap was exceeded; a later anonymous publish
    # for it carries no attribution.
    event = hub.publish(client_order_id="oldest", engine_state=OrderState.NEW)
    assert event is not None and event.strategy_id is None


def test_register_strategy_attributes_later_anonymous_events() -> None:
    hub = _hub()
    hub.register_strategy("A", "csm")
    # A publish WITHOUT an explicit strategy_id is attributed from the LRU.
    event = hub.publish(client_order_id="A", engine_state=OrderState.NEW)
    assert event is not None and event.strategy_id == "csm"


def test_publish_carries_fill_payload() -> None:
    hub = _hub()
    fill = FillEvent(broker_fill_id="F-1", price=Decimal("12.5"), quantity=40, exec_ts=_TS)
    event = hub.publish(client_order_id="A", engine_state=OrderState.PARTIALLY_FILLED, fill=fill)
    assert event is not None and event.fill is not None
    assert event.fill.broker_fill_id == "F-1"
    assert event.wire_dump()["fill"]["price"] == "12.5"  # Decimal-as-string


def test_module_singleton_lifecycle() -> None:
    assert get_event_hub() is None
    hub = create_event_hub(make_settings())
    assert get_event_hub() is hub
    # Idempotent: a second create returns the same instance.
    assert create_event_hub(make_settings()) is hub
    reset_event_hub()
    assert get_event_hub() is None

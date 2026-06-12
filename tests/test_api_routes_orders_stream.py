"""GET /orders/stream (3G): live filtering, replay, resync, keep-alive, gap, seed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi.testclient import TestClient
from src.quant_execution_engine.api.streams import _order_stream_events, order_stream
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.events.hub import EventHub, create_event_hub

from tests._fakes import FakeConn, FakePool
from tests.conftest import build_client, make_settings


def _hub(**setting_overrides: Any) -> EventHub:
    return create_event_hub(make_settings(**setting_overrides))


async def _drain(gen: AsyncGenerator[str, None], count: int, *, timeout: float = 1.0) -> list[str]:
    """Pull ``count`` frames from the SSE generator (bounded by ``timeout``)."""
    frames: list[str] = []
    for _ in range(count):
        frames.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
    return frames


async def _next_after(gen: AsyncGenerator[str, None], publish: Any, *, timeout: float = 1.0) -> str:
    """Prime the generator's subscription, run ``publish``, then read one frame.

    The generator only enters ``hub.subscribe()`` on the first ``__anext__``, so
    a publish issued before that would miss the live tap. Start the pull as a
    task, yield once to let it subscribe, publish, then await the frame.
    """
    pending = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    publish()
    return await asyncio.wait_for(pending, timeout=timeout)


def _frame_data(frame: str) -> dict[str, Any]:
    """Parse the ``data:`` payload of an event frame."""
    for line in frame.splitlines():
        if line.startswith("data: "):
            data: dict[str, Any] = json.loads(line[len("data: ") :])
            return data
    raise AssertionError(f"no data line in frame: {frame!r}")


def _client(**setting_overrides: Any) -> TestClient:
    settings = make_settings(**setting_overrides)
    client, _ = build_client(settings=settings, pool=object(), redis=None)
    return client


# ----------------------------------------------------------------- auth / 503


def test_stream_503_when_hub_not_running() -> None:
    # No hub created (lifespan not run) ⇒ typed 503 order_stream_unavailable.
    client = _client()
    response = client.get("/orders/stream")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "order_stream_unavailable"


def test_stream_api_key_enforced_when_configured() -> None:
    _hub()
    client = _client(api_key="sekret")
    assert client.get("/orders/stream").status_code == 401


def test_stream_invalid_last_event_id_422() -> None:
    _hub()
    client = _client()
    assert client.get("/orders/stream", params={"last_event_id": "nope"}).status_code == 422


def test_stream_invalid_last_event_id_header_422() -> None:
    _hub()
    client = _client()
    response = client.get("/orders/stream", headers={"Last-Event-ID": "xyz"})
    assert response.status_code == 422


# ------------------------------------------------------------------ live flow


async def test_live_event_reaches_matching_strategy_only() -> None:
    hub = _hub()
    gen = _order_stream_events(
        hub,
        after_seq=0,
        strategy_id="csm",
        client_order_id=None,
        seeded=set(),
        keepalive_seconds=30,
    )

    def _publish() -> None:
        hub.publish(client_order_id="A", engine_state=OrderState.NEW, strategy_id="csm")
        hub.publish(client_order_id="B", engine_state=OrderState.NEW, strategy_id="tfex")

    try:
        frame = await _next_after(gen, _publish)
        data = _frame_data(frame)
        assert data["client_order_id"] == "A"  # the tfex event was filtered out
        assert frame.startswith("id: ")
        assert "event: NEW" in frame
    finally:
        await gen.aclose()


async def test_client_order_id_filter() -> None:
    hub = _hub()
    gen = _order_stream_events(
        hub,
        after_seq=0,
        strategy_id=None,
        client_order_id="B",
        seeded=set(),
        keepalive_seconds=30,
    )

    def _publish() -> None:
        hub.publish(client_order_id="A", engine_state=OrderState.NEW)
        hub.publish(client_order_id="B", engine_state=OrderState.FILLED)

    try:
        frame = await _next_after(gen, _publish)
        assert _frame_data(frame)["client_order_id"] == "B"
    finally:
        await gen.aclose()


async def test_conjunctive_filters() -> None:
    hub = _hub()
    gen = _order_stream_events(
        hub,
        after_seq=0,
        strategy_id="csm",
        client_order_id="B",
        seeded=set(),
        keepalive_seconds=30,
    )

    def _publish() -> None:
        # Right strategy, wrong cid -> filtered.
        hub.publish(client_order_id="A", engine_state=OrderState.NEW, strategy_id="csm")
        # Both match.
        hub.publish(client_order_id="B", engine_state=OrderState.NEW, strategy_id="csm")

    try:
        frame = await _next_after(gen, _publish)
        assert _frame_data(frame)["client_order_id"] == "B"
    finally:
        await gen.aclose()


# --------------------------------------------------------------- replay/resync


async def test_last_event_id_replays_then_live() -> None:
    hub = _hub()
    for state in (OrderState.PENDING_NEW, OrderState.NEW, OrderState.FILLED):
        hub.publish(client_order_id="A", engine_state=state)
    gen = _order_stream_events(
        hub,
        after_seq=1,  # replay seq 2,3 then live
        strategy_id=None,
        client_order_id=None,
        seeded=set(),
        keepalive_seconds=30,
    )
    try:
        replayed = await _drain(gen, 2)
        assert [f.split("\n")[0] for f in replayed] == ["id: 2", "id: 3"]
        # A new live event flows after the replay (deduped against max replayed seq).
        hub.publish(client_order_id="A", engine_state=OrderState.CANCELLED)
        live = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert live.split("\n")[0] == "id: 4"
    finally:
        await gen.aclose()


async def test_resync_required_when_cursor_fell_off_a_tiny_ring() -> None:
    hub = _hub(stream_ring_buffer_size=2)
    for _ in range(6):
        hub.publish(client_order_id="A", engine_state=OrderState.NEW)
    gen = _order_stream_events(
        hub,
        after_seq=1,  # long gone from the 2-slot ring
        strategy_id=None,
        client_order_id=None,
        seeded=set(),
        keepalive_seconds=30,
    )
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert first.startswith("event: resync_required")
        assert json.loads(first.splitlines()[1][len("data: ") :])["after_seq"] == 1
    finally:
        await gen.aclose()


# --------------------------------------------------------- keep-alive / gap


async def test_keepalive_frame_on_idle() -> None:
    hub = _hub()
    gen = _order_stream_events(
        hub,
        after_seq=0,
        strategy_id=None,
        client_order_id=None,
        seeded=set(),
        keepalive_seconds=0.01,
    )
    try:
        frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
        assert frame == ": keep-alive\n\n"
    finally:
        await gen.aclose()


async def test_gap_frame_after_overflow() -> None:
    hub = _hub(stream_subscriber_queue_size=2)
    gen = _order_stream_events(
        hub,
        after_seq=0,
        strategy_id=None,
        client_order_id=None,
        seeded=set(),
        keepalive_seconds=0.05,  # a short keep-alive bounds the drain loop
    )
    # __anext__ once to enter the subscription (subscribe-then-replay), then
    # overflow the queue before reading further.
    pending = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)  # let the generator subscribe
    try:
        for _ in range(6):
            hub.publish(client_order_id="A", engine_state=OrderState.NEW)
        # Drain until a keep-alive frame proves the queue is empty.
        frames = [await asyncio.wait_for(pending, timeout=1.0)]
        while True:
            frame = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
            if frame == ": keep-alive\n\n":
                break
            frames.append(frame)
        gap_frames = [f for f in frames if f.startswith("event: gap")]
        assert gap_frames, f"expected a gap frame, got {frames!r}"
        assert json.loads(gap_frames[0].splitlines()[1][len("data: ") :])["dropped"] > 0
    finally:
        if not pending.done():
            pending.cancel()
        await gen.aclose()


# ------------------------------------------------------------ DB-seeded path


async def test_seeded_set_attributes_anonymous_events_post_restart() -> None:
    # Fresh hub (simulating post-restart: empty LRU) — events published WITHOUT a
    # strategy_id still reach the subscriber whose seeded set contains the cid.
    hub = _hub()
    gen = _order_stream_events(
        hub,
        after_seq=0,
        strategy_id="csm",
        client_order_id=None,
        seeded={"A"},  # loaded from the durable store at subscribe time
        keepalive_seconds=30,
    )
    try:
        frame = await _next_after(
            gen,
            lambda: hub.publish(client_order_id="A", engine_state=OrderState.NEW),
        )
        assert _frame_data(frame)["client_order_id"] == "A"
    finally:
        await gen.aclose()


def test_stream_loads_seed_set_for_strategy_filter() -> None:
    # The route loads the seed set via the repository when strategy_id is given.
    _hub()
    conn = FakeConn(fetch_results=[[{"client_order_id": "A"}]])
    pool = FakePool(conn)

    async def _run() -> None:
        from starlette.requests import Request

        scope = {"type": "http", "headers": [], "method": "GET", "query_string": b""}
        request = Request(scope)
        response = await order_stream(
            request,
            make_settings(),
            pool,
            strategy_id="csm",
            client_order_id=None,
            last_event_id=None,
        )
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

    asyncio.run(_run())
    # The seed SELECT ran with the strategy id.
    assert any("strategy_id = $1" in sql for _, sql, _ in conn.calls)

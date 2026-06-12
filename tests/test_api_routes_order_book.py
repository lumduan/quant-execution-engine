"""Order-book REST + SSE routes (3E): snapshot, stream, /health block."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.quant_execution_engine.api.streams import _order_book_events, order_book_stream
from src.quant_execution_engine.contracts.enums import Market
from src.quant_execution_engine.order_book import runtime as order_book_runtime
from src.quant_execution_engine.order_book.models import (
    OrderBook,
    OrderBookLevel,
    OrderBookSource,
)
from src.quant_execution_engine.order_book.service import OrderBookService

from tests.conftest import build_client, make_settings


class _FakeRouter:
    """A ProviderRouter stand-in: records subscribe/unsubscribe + health fields."""

    def __init__(self) -> None:
        self.subscribed: list[tuple[str, Market]] = []
        self.unsubscribed: list[tuple[str, Market]] = []
        self.active = OrderBookSource.SETTRADE

    @property
    def providers(self) -> tuple[Any, ...]:
        class _P:
            name = OrderBookSource.SETTRADE

        class _Q:
            name = OrderBookSource.LIBERATOR

        return (_P(), _Q())

    async def subscribe(self, symbol: str, market: Market) -> None:
        self.subscribed.append((symbol, market))

    async def unsubscribe(self, symbol: str, market: Market) -> None:
        self.unsubscribed.append((symbol, market))


def _book(
    symbol: str = "PTT", market: Market = Market.SET, *, received_at: datetime | None = None
) -> OrderBook:
    return OrderBook(
        symbol=symbol,
        market=market,
        bid_levels=[OrderBookLevel(price=Decimal("99.5"), volume=10)],
        ask_levels=[OrderBookLevel(price=Decimal("100.5"), volume=8)],
        sequence=1,
        source=OrderBookSource.SETTRADE,
        received_at=received_at or datetime.now(UTC),
    )


def _install_service(**overrides: Any) -> tuple[OrderBookService, _FakeRouter]:
    """Wire a real service over a fake router into the runtime singletons."""
    router = _FakeRouter()
    kwargs: dict[str, Any] = {
        "router": router,
        "max_symbols": 500,
        "max_age_seconds": 5,
        "subscriber_queue_size": 256,
    }
    kwargs.update(overrides)
    service = OrderBookService(**kwargs)
    order_book_runtime._service = service
    order_book_runtime._router = router  # type: ignore[assignment]
    return service, router


def _client(**setting_overrides: Any) -> TestClient:
    settings = make_settings(**setting_overrides)
    client, _ = build_client(settings=settings, pool=object(), redis=None)
    return client


# ------------------------------------------------------------------ snapshot


def test_snapshot_404_when_disabled() -> None:
    client = _client()  # no service installed ⇒ disabled
    response = client.get("/order-book/PTT")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "order_book_unavailable"


def test_snapshot_404_when_cold() -> None:
    _install_service()
    client = _client()
    response = client.get("/order-book/PTT")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "order_book_unavailable"


def test_snapshot_200_with_wire_shape_when_warm() -> None:
    service, _ = _install_service()
    service.publish(_book())
    client = _client()
    response = client.get("/order-book/PTT", params={"market": "SET"})
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "PTT"
    assert body["market"] == "SET"
    assert body["bid_levels"][0]["price"] == "99.5"  # Decimal-as-string
    assert isinstance(body["ask_levels"][0]["price"], str)
    assert body["source"] == "settrade"


def test_snapshot_market_none_probes_set_then_tfex() -> None:
    service, _ = _install_service()
    # Only a TFEX book exists; market omitted must probe SET (miss) then TFEX (hit).
    service.publish(_book(symbol="S50M26", market=Market.TFEX))
    client = _client()
    response = client.get("/order-book/S50M26")
    assert response.status_code == 200
    assert response.json()["market"] == "TFEX"


def test_snapshot_stale_book_reads_as_absent() -> None:
    service, _ = _install_service(max_age_seconds=0)
    service.publish(_book(received_at=datetime(2000, 1, 1, tzinfo=UTC)))
    client = _client()
    assert client.get("/order-book/PTT", params={"market": "SET"}).status_code == 404


# ---------------------------------------------------------------------- SSE


def test_stream_404_when_disabled() -> None:
    client = _client()
    response = client.get("/order-book/PTT/stream", params={"market": "SET"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "order_book_unavailable"


def test_stream_requires_market_param() -> None:
    _install_service()
    client = _client()
    # market is REQUIRED ⇒ 422 when omitted (FastAPI validation).
    assert client.get("/order-book/PTT/stream").status_code == 422


async def test_stream_response_headers_present() -> None:
    # The StreamingResponse carries the no-buffering headers (D24); inspected
    # directly so the infinite body is never consumed (TestClient would block).
    service, _ = _install_service()
    service.publish(_book())
    settings = make_settings(stream_keepalive_seconds=30)
    response = await order_book_stream("PTT", Market.SET, settings)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


async def test_stream_snapshot_then_update_and_release() -> None:
    # Drive the SSE generator directly (in-loop) — the TestClient runs it on a
    # separate portal loop, so a cross-thread publish never wakes queue.get().
    service, router = _install_service()
    service.publish(_book())  # warm cache ⇒ first frame is the snapshot
    gen: AsyncGenerator[str, None] = _order_book_events(
        service, "PTT", Market.SET, keepalive_seconds=30
    )
    first = await gen.__anext__()
    assert first.startswith("data: ")
    assert json.loads(first[len("data: ") :])["bid_levels"][0]["price"] == "99.5"
    assert router.subscribed == [("PTT", Market.SET)]
    # A published update arrives as the next frame.
    service.publish(_book(received_at=datetime.now(UTC)))
    second = await gen.__anext__()
    assert json.loads(second[len("data: ") :])["symbol"] == "PTT"
    # Closing the generator (client disconnect) releases the subscription.
    await gen.aclose()
    assert router.unsubscribed == [("PTT", Market.SET)]
    assert service.subscriber_count == 0


async def test_stream_keepalive_comment_when_idle() -> None:
    # No warm cache ⇒ no snapshot; a tiny keepalive ⇒ the comment frame fires.
    service, _ = _install_service()
    gen: AsyncGenerator[str, None] = _order_book_events(
        service, "PTT", Market.SET, keepalive_seconds=0.01
    )
    frame = await gen.__anext__()
    assert frame == ": keep-alive\n\n"
    await gen.aclose()


# -------------------------------------------------------------------- /health


def test_health_order_book_none_when_disabled() -> None:
    client = _client()
    body = client.get("/health").json()
    assert body["order_book"] is None


def test_health_order_book_block_when_enabled() -> None:
    service, _ = _install_service()
    service.publish(_book())
    client = _client()
    body = client.get("/health").json()
    ob = body["order_book"]
    assert ob["active_provider"] == "settrade"
    assert ob["providers"] == ["settrade", "liberator"]
    assert ob["cached_symbols"] == 1
    assert ob["subscribers"] == 0


def test_snapshot_readable_in_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # public_mode default True must NOT block the GET (D24).
    service, _ = _install_service()
    service.publish(_book())
    client = _client(public_mode=True)
    assert client.get("/order-book/PTT", params={"market": "SET"}).status_code == 200

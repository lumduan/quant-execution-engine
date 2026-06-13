"""Audit read + NDJSON export (Phase 6 / E1-E2): synthesis, filters, owner-mode.

E1 ``GET /admin/orders/{cid}/audit`` and E2 ``GET /admin/audit/export`` are
owner-mode reads synthesized from the EXISTING ``execution.order_events`` columns
(Design Decision §3 — no schema change). Tests drive the HTTP surface over the
MemStore (which mirrors the append-only audit trigger) plus the repository readers
directly (the event_type mapping is unit-tested as a pure function, and the export
cursor path is exercised through a FakeConn so the streaming — not buffered — path
is proven).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.quant_execution_engine.api.audit import _to_utc_iso, event_type_for
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.db import repositories
from src.quant_execution_engine.db.models import OrderEventRow

from tests._fakes import FakeConn, FakePool, FakeRedis, MemStore, patch_repositories
from tests.conftest import build_client, make_settings, order_payload


def _owner_client(
    monkeypatch: pytest.MonkeyPatch, *, redis: Any | None = None, **overrides: Any
) -> tuple[TestClient, MemStore]:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    settings: Settings = make_settings(public_mode=False, **overrides)
    client, _ = build_client(settings=settings, pool=object(), redis=redis)
    return client, store


# ---------------------------------------------------------- event_type mapping


@pytest.mark.parametrize(
    ("from_status", "to_status", "expected"),
    [
        (None, OrderState.PENDING_NEW, "create"),
        (OrderState.PENDING_NEW, OrderState.NEW, "ack"),
        (OrderState.PENDING_REPLACE, OrderState.NEW, "replace"),
        (OrderState.NEW, OrderState.PARTIALLY_FILLED, "fill"),
        (OrderState.PARTIALLY_FILLED, OrderState.FILLED, "fill"),
        (OrderState.NEW, OrderState.FILLED, "fill"),
        (OrderState.NEW, OrderState.PENDING_CANCEL, "cancel_request"),
        (OrderState.PENDING_CANCEL, OrderState.CANCELLED, "cancel"),
        (OrderState.NEW, OrderState.PENDING_REPLACE, "replace_request"),
        (OrderState.PENDING_NEW, OrderState.REJECTED, "reject"),
        (OrderState.NEW, OrderState.EXPIRED, "expire"),
    ],
)
def test_event_type_for_covers_every_frozen_edge(
    from_status: OrderState | None, to_status: OrderState, expected: str
) -> None:
    assert event_type_for(from_status, to_status) == expected


def test_to_utc_iso_normalizes_naive_and_aware() -> None:
    naive = datetime(2026, 6, 12, 10, 0, 0)  # noqa: DTZ001 - deliberately naive
    assert _to_utc_iso(naive).endswith("+00:00")
    aware = datetime(2026, 6, 12, 17, 0, 0, tzinfo=UTC)
    assert _to_utc_iso(aware) == "2026-06-12T17:00:00+00:00"


# --------------------------------------------------------------- E1 audit read


def test_audit_read_full_sequence_for_a_sim_filled_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = _owner_client(monkeypatch)
    body = order_payload()
    assert client.post("/orders", json=body).status_code == 201  # sim FILLED end-to-end
    cid = body["client_order_id"]

    response = client.get(f"/admin/orders/{cid}/audit")
    assert response.status_code == 200
    payload = response.json()
    assert payload["client_order_id"] == cid
    assert payload["broker"] == "sim"
    assert payload["symbol"] == "PTT"
    events = payload["events"]
    # seq is a 1-based monotonic ordinal.
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    # The sim path: create -> ack -> fill (PENDING_NEW birth, ack, the FILLED fill).
    types = [e["event_type"] for e in events]
    assert types[0] == "create"
    assert "ack" in types
    assert types[-1] == "fill"
    # occurred_at is UTC ISO-8601; metadata is the opaque event JSONB; the ack row
    # surfaces the broker_order_id snapshot.
    assert all(e["occurred_at"].endswith("+00:00") for e in events)
    ack_event = next(e for e in events if e["event_type"] == "ack")
    assert ack_event["broker_order_id"] is not None
    assert ack_event["broker_order_id"] == ack_event["metadata"]["broker_order_id"]


def test_audit_read_surfaces_kill_switch_cancel_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design §6: the kill-switch CANCELLED rows surface as cancel_request/cancel."""
    from src.quant_execution_engine.contracts.orders import NormalizedOrder

    client, store = _owner_client(monkeypatch, redis=FakeRedis())
    order = NormalizedOrder(**order_payload())
    store.seed(order, OrderState.NEW)  # a resting order to sweep
    cid = order.client_order_id

    engage = client.post("/admin/kill-switch/engage")
    assert engage.status_code == 200
    assert engage.json()["cancelled_count"] == 1
    assert store.orders[cid]["status"] is OrderState.CANCELLED

    events = client.get(f"/admin/orders/{cid}/audit").json()["events"]
    types = [e["event_type"] for e in events]
    # The mass-cancel sweep drives NEW -> PENDING_CANCEL -> CANCELLED.
    assert "cancel_request" in types
    assert types[-1] == "cancel"


def test_audit_read_unknown_cid_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _owner_client(monkeypatch)
    response = client.get(f"/admin/orders/{uuid4()}/audit")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "order_not_found"


def test_audit_read_403_in_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    client, _ = build_client(settings=make_settings(), pool=object(), redis=None)  # public
    response = client.get(f"/admin/orders/{uuid4()}/audit")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "public_mode"


def test_audit_read_api_key_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _owner_client(monkeypatch, api_key="sekret")
    assert client.get(f"/admin/orders/{uuid4()}/audit").status_code == 401
    ok = client.get(f"/admin/orders/{uuid4()}/audit", headers={"X-API-Key": "sekret"})
    assert ok.status_code == 404  # auth passed; cid simply unknown


# ------------------------------------------------------------ E2 NDJSON export


def _ndjson_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line]


def test_export_streams_exactly_one_line_per_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.quant_execution_engine.contracts.orders import NormalizedOrder

    client, store = _owner_client(monkeypatch, redis=FakeRedis())
    # Build a deterministic 10-event fixture: seed 4 resting orders (1 birth row
    # each = 4) and cancel 3 of them (NEW -> PENDING_CANCEL -> CANCELLED, 2 rows
    # each = 6) → exactly 10 append-only audit rows.
    orders = [NormalizedOrder(**order_payload()) for _ in range(4)]
    for order in orders:
        store.seed(order, OrderState.NEW)
    for order in orders[:3]:
        assert client.delete(f"/orders/{order.client_order_id}").status_code == 200

    assert len(store.events) == 10
    response = client.get("/admin/audit/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    rows = _ndjson_lines(response.text)
    assert len(rows) == 10
    # Each line carries the required fields (+ strategy_id from the join).
    first = rows[0]
    for key in ("event_id", "client_order_id", "from_status", "to_status", "event", "created_at"):
        assert key in first
    assert "strategy_id" in first
    # event is a real object, not a quoted string.
    assert isinstance(first["event"], dict)
    # event_id is globally monotonic across the export.
    assert [r["event_id"] for r in rows] == sorted(r["event_id"] for r in rows)


def test_export_content_disposition_uses_all_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _owner_client(monkeypatch)
    response = client.get("/admin/audit/export")
    assert response.headers["content-disposition"] == 'attachment; filename="audit_all_all.ndjson"'


def test_export_content_disposition_names_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _owner_client(monkeypatch)
    response = client.get(
        "/admin/audit/export",
        params={"from_ts": "2026-06-01T00:00:00Z", "to_ts": "2026-06-30T00:00:00Z"},
    )
    cd = response.headers["content-disposition"]
    assert cd == 'attachment; filename="audit_2026-06-01_2026-06-30.ndjson"'


def test_export_strategy_id_filter_only_matching_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store = _owner_client(monkeypatch)
    csm = order_payload()
    other = order_payload()
    assert client.post("/orders", json=csm, headers={"X-Strategy-Id": "csm"}).status_code == 201
    assert client.post("/orders", json=other).status_code == 201  # anonymous

    response = client.get("/admin/audit/export", params={"strategy_id": "csm"})
    rows = _ndjson_lines(response.text)
    assert rows  # csm has rows
    assert {r["client_order_id"] for r in rows} == {csm["client_order_id"]}
    assert all(r["strategy_id"] == "csm" for r in rows)


def test_export_403_in_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    client, _ = build_client(settings=make_settings(), pool=object(), redis=None)  # public
    response = client.get("/admin/audit/export")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "public_mode"


# --------------------------------- repository readers / cursor streaming path


async def test_fetch_order_events_orders_by_event_id_and_decodes_json() -> None:
    # event arrives as a JSON string from asyncpg (no codec) — the reader decodes it.
    conn = FakeConn(
        fetch_results=[
            [
                {
                    "event_id": 1,
                    "from_status": None,
                    "to_status": "PENDING_NEW",
                    "event": json.dumps({"broker_order_id": None, "price": "10", "quantity": 100}),
                    "created_at": datetime(2026, 6, 12, tzinfo=UTC),
                },
                {
                    "event_id": 2,
                    "from_status": "PENDING_NEW",
                    "to_status": "NEW",
                    "event": {"broker_order_id": "SIM-1", "price": "10", "quantity": 100},
                    "created_at": datetime(2026, 6, 12, tzinfo=UTC),
                },
            ]
        ]
    )
    rows = await repositories.fetch_order_events(FakePool(conn), "cid")
    assert [r.event_id for r in rows] == [1, 2]
    assert rows[0].from_status is None
    assert rows[0].event == {"broker_order_id": None, "price": "10", "quantity": 100}
    assert rows[1].event is not None and rows[1].event["broker_order_id"] == "SIM-1"
    # The SELECT is ordered by event_id.
    assert "ORDER BY event_id" in conn.calls[0][1]


async def test_stream_order_events_uses_server_side_cursor_not_fetch() -> None:
    rows = [
        {
            "event_id": 1,
            "client_order_id": "A",
            "from_status": None,
            "to_status": "PENDING_NEW",
            "event": json.dumps({"broker_order_id": None, "price": "10", "quantity": 100}),
            "created_at": datetime(2026, 6, 12, tzinfo=UTC),
            "strategy_id": "csm",
        }
    ]
    conn = FakeConn(cursor_results=[rows])
    pool = FakePool(conn)
    out = [r async for r in repositories.stream_order_events(pool)]
    assert len(out) == 1
    assert out[0]["client_order_id"] == "A"
    assert out[0]["strategy_id"] == "csm"
    assert out[0]["event"] == {"broker_order_id": None, "price": "10", "quantity": 100}
    assert out[0]["created_at"] == "2026-06-12T00:00:00+00:00"
    # The streaming (cursor) path ran inside a transaction — never a buffered fetch.
    assert conn.transaction_entered == 1
    assert conn.cursors and conn.cursors[0].iterated
    assert not any(method == "fetch" for method, _, _ in conn.calls)


async def test_stream_order_events_filters_bind_positional_args() -> None:
    conn = FakeConn(cursor_results=[[]])
    pool = FakePool(conn)
    out = [
        r
        async for r in repositories.stream_order_events(
            pool,
            from_ts=datetime(2026, 6, 1, tzinfo=UTC),
            to_ts=datetime(2026, 6, 30, tzinfo=UTC),
            strategy_id="csm",
        )
    ]
    assert out == []
    method, sql, args = conn.calls[0]
    assert method == "cursor"
    assert "e.created_at >= $1" in sql
    assert "e.created_at < $2" in sql
    assert "o.strategy_id = $3" in sql
    assert "ORDER BY e.event_id" in sql
    assert args == (
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 30, tzinfo=UTC),
        "csm",
    )


def test_order_event_row_from_record_passthrough_when_event_none() -> None:
    row = OrderEventRow.from_record(
        {
            "event_id": 7,
            "from_status": "NEW",
            "to_status": "CANCELLED",
            "event": None,
            "created_at": datetime(2026, 6, 12, tzinfo=UTC),
        }
    )
    assert row.event is None
    assert row.to_status is OrderState.CANCELLED

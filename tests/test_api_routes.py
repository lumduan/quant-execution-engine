"""API surface: modes, auth, envelopes, lifecycle through HTTP."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.events.hub import create_event_hub

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import build_client, make_settings, order_payload


def _owner_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    redis: Any | None = None,
    **overrides: Any,
) -> tuple[TestClient, MemStore, Any]:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    redis = FakeRedis() if redis is None else redis
    settings: Settings = make_settings(public_mode=False, **overrides)
    client, _ = build_client(settings=settings, pool=object(), redis=redis)
    return client, store, redis


# ----------------------------------------------------------------- public mode


def test_public_mode_blocks_writes_allows_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    client, _ = build_client(settings=make_settings(), pool=object(), redis=FakeRedis())
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["public_mode"] is True
    assert client.get("/capabilities").status_code == 200
    assert client.get("/orders/some-id").status_code == 404  # read path is open
    for method, path in [
        ("post", "/orders"),
        ("delete", "/orders/x"),
        ("get", "/admin/kill-switch"),
        ("post", "/admin/kill-switch/engage"),
        ("post", "/admin/kill-switch/disengage"),
    ]:
        response = getattr(client, method)(
            path, **({"json": order_payload()} if method == "post" and path == "/orders" else {})
        )
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "public_mode"
    assert store.orders == {}


# ------------------------------------------------------------------- API key


def test_api_key_enforced_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _owner_client(monkeypatch, api_key="sekret")
    assert client.get("/capabilities").status_code == 401
    assert client.get("/capabilities", headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.get("/capabilities", headers={"X-API-Key": "sekret"})
    assert ok.status_code == 200


# ------------------------------------------------------------------ lifecycle


def test_submit_lifecycle_dedupe_and_decimal_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = _owner_client(monkeypatch)
    body = order_payload()
    first = client.post("/orders", json=body)
    assert first.status_code == 201
    payload = first.json()
    assert payload["status"] == "FILLED"
    assert payload["engine_state"] == "FILLED"
    assert payload["avg_fill_price"] == "123.456789"
    assert isinstance(payload["avg_fill_price"], str)
    assert "raw" not in payload
    resend = client.post("/orders", json=body)
    assert resend.status_code == 200
    assert resend.json() == payload
    assert len(store.orders) == 1
    read = client.get(f"/orders/{body['client_order_id']}")
    assert read.status_code == 200
    assert read.json() == payload


def test_amend_native_happy_path_same_cid(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATCH a resting settrade order: 200, same cid, updated price."""
    client, store, _ = _owner_client(monkeypatch)
    body = order_payload(broker="settrade", metadata={"sim_fills": []})
    assert client.post("/orders", json=body).status_code == 201
    cid = body["client_order_id"]

    amended = client.patch(f"/orders/{cid}", json={"new_price": "150.25", "new_qty": 80})
    assert amended.status_code == 200
    payload = amended.json()
    assert payload["client_order_id"] == cid  # native keeps the same id
    assert payload["engine_state"] == "NEW"
    assert store.orders[cid]["price"] == Decimal("150.25")
    assert store.orders[cid]["quantity"] == 80


def test_amend_unknown_cid_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _owner_client(monkeypatch)
    response = client.patch(f"/orders/{uuid4()}", json={"new_price": "10"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "order_not_found"


def test_amend_no_change_body_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _owner_client(monkeypatch)
    response = client.patch(f"/orders/{uuid4()}", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_amend_float_price_rejected_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _owner_client(monkeypatch)
    response = client.patch(f"/orders/{uuid4()}", json={"new_price": 10.5})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_amend_native_with_new_cid_409_amend_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native broker must omit new_client_order_id — typed 409 envelope."""
    client, store, _ = _owner_client(monkeypatch)
    body = order_payload(broker="settrade", metadata={"sim_fills": []})
    assert client.post("/orders", json=body).status_code == 201
    response = client.patch(
        f"/orders/{body['client_order_id']}",
        json={"new_price": "150.25", "new_client_order_id": str(uuid4())},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "amend_rejected"


def test_amend_owner_mode_and_api_key_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    # Public mode blocks the write (mirror DELETE).
    store = MemStore()
    patch_repositories(monkeypatch, store)
    public = build_client(settings=make_settings(), pool=object(), redis=FakeRedis())[0]
    blocked = public.patch(f"/orders/{uuid4()}", json={"new_price": "1"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "public_mode"

    # API key enforced when configured.
    keyed, _, _ = _owner_client(monkeypatch, api_key="sekret")
    no_key = keyed.patch(f"/orders/{uuid4()}", json={"new_price": "1"})
    assert no_key.status_code == 401


def test_partial_fill_then_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _owner_client(monkeypatch)
    body = order_payload(metadata={"sim_fills": []})
    assert client.post("/orders", json=body).status_code == 201
    cancelled = client.delete(f"/orders/{body['client_order_id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["engine_state"] == "CANCELLED"
    again = client.delete(f"/orders/{body['client_order_id']}")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "illegal_transition"


# -------------------------------------------------------------- typed errors


def test_typed_error_envelopes(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _owner_client(monkeypatch, risk_max_order_qty=10)
    over_cap = client.post("/orders", json=order_payload(quantity=11))
    assert over_cap.status_code == 422
    assert over_cap.json()["error"]["code"] == "risk_rejected"

    unsupported = client.post(
        "/orders",
        json=order_payload(broker="liberator", order_type="STOP", stop_price="9"),
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "capability_unsupported"

    not_uuid = client.post("/orders", json=order_payload(client_order_id="nope"))
    assert not_uuid.status_code == 422
    assert not_uuid.json()["error"]["code"] == "validation_error"

    missing = client.get("/orders/unknown-id")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "order_not_found"


def test_rate_limit_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-second cap keys on ``exe:rate:{int(time.time())}`` (core.risk). Freeze
    # that clock so the two posts can't straddle an integer-second boundary into two
    # separate windows — the source of the CI flakiness (the second post then lands in
    # a fresh window with count 1 and isn't throttled).
    monkeypatch.setattr("src.quant_execution_engine.core.risk.time.time", lambda: 1_700_000_000.0)
    client, _, _ = _owner_client(monkeypatch, risk_max_orders_per_second=1)
    assert client.post("/orders", json=order_payload(symbol="A")).status_code == 201
    throttled = client.post("/orders", json=order_payload(symbol="B"))
    assert throttled.status_code == 429
    assert throttled.json()["error"]["detail"]["cap"] == "rate_limit"


def test_stage_gate_rejects_when_no_real_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _owner_client(monkeypatch, stage="live")
    response = client.post("/orders", json=order_payload())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "stage_rejected"


# ----------------------------------------------------------------- kill-switch


def test_kill_switch_admin_engage_mass_cancel_disengage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = _owner_client(monkeypatch)
    resting = order_payload(metadata={"sim_fills": []})
    assert client.post("/orders", json=resting).status_code == 201

    state = client.get("/admin/kill-switch")
    assert state.status_code == 200
    assert state.json() == {"engaged": False, "source": None}

    engaged = client.post("/admin/kill-switch/engage")
    assert engaged.status_code == 200
    body = engaged.json()
    assert body["engaged"] is True
    assert body["already_engaged"] is False
    assert body["cancelled_count"] == 1
    assert body["cancelled"] == [resting["client_order_id"]]
    assert body["failed"] == []

    rejected = client.post("/orders", json=order_payload())
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "kill_switch_engaged"

    disengaged = client.post("/admin/kill-switch/disengage")
    assert disengaged.status_code == 200
    assert disengaged.json()["engaged"] is False
    assert client.post("/orders", json=order_payload(symbol="OK")).status_code == 201


def test_kill_switch_engage_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second engage returns already_engaged=true and runs NO second sweep."""
    client, store, _ = _owner_client(monkeypatch)
    resting = order_payload(metadata={"sim_fills": []})
    assert client.post("/orders", json=resting).status_code == 201

    first = client.post("/admin/kill-switch/engage")
    assert first.status_code == 200
    assert first.json()["already_engaged"] is False
    assert first.json()["cancelled_count"] == 1
    assert store.orders[resting["client_order_id"]]["status"] == "CANCELLED"

    second = client.post("/admin/kill-switch/engage")
    assert second.status_code == 200
    body = second.json()
    assert body["engaged"] is True
    assert body["already_engaged"] is True
    assert body["cancelled_count"] == 0  # no re-sweep
    assert body["cancelled"] == [] and body["failed"] == []


def test_kill_switch_disengage_when_not_engaged_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disengage while clear → 409 kill_switch_not_engaged (distinct from pinned)."""
    client, _, _ = _owner_client(monkeypatch)
    response = client.post("/admin/kill-switch/disengage")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "kill_switch_not_engaged"


def test_kill_switch_engage_disengage_403_in_public_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    public, _ = build_client(settings=make_settings(), pool=object(), redis=FakeRedis())
    for path in ("/admin/kill-switch/engage", "/admin/kill-switch/disengage"):
        response = public.post(path)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "public_mode"


def test_kill_switch_operator_id_is_optional_and_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """X-Operator-Id is optional (never required) and lands in the structured log."""
    client, _, _ = _owner_client(monkeypatch)
    logger_name = "src.quant_execution_engine.api.routes"

    # Absent header → no 4xx, and the audit log records "anonymous".
    with caplog.at_level(logging.INFO, logger=logger_name):
        anon = client.post("/admin/kill-switch/engage")
    assert anon.status_code == 200
    engaged_logs = [r.message for r in caplog.records if "kill_switch.engaged" in r.message]
    assert engaged_logs and '"operator": "anonymous"' in engaged_logs[-1]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger_name):
        client.post("/admin/kill-switch/disengage", headers={"X-Operator-Id": "ops-jane"})
    disengaged_logs = [r.message for r in caplog.records if "kill_switch.disengaged" in r.message]
    assert disengaged_logs and '"operator": "ops-jane"' in disengaged_logs[-1]


def test_kill_switch_fault_injection_five_orders_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: 5 orders (NEW + PARTIALLY_FILLED) → engage flattens all + audit trail;
    fresh submit rejected; disengage → fresh submit accepted.
    """
    hub = create_event_hub(make_settings())  # captures the genuine CANCELLED edges
    client, store, _ = _owner_client(monkeypatch, risk_max_orders_per_second=100)

    # 3 resting at NEW, 2 resting PARTIALLY_FILLED — a deliberate mix of live states.
    new_cids = [order_payload(symbol=f"NEW{i}", metadata={"sim_fills": []}) for i in range(3)]
    partial_cids = [
        order_payload(symbol=f"PF{i}", quantity=100, metadata={"sim_fills": [40]}) for i in range(2)
    ]
    for body in (*new_cids, *partial_cids):
        assert client.post("/orders", json=body).status_code == 201
    cids = [b["client_order_id"] for b in (*new_cids, *partial_cids)]
    assert store.orders[partial_cids[0]["client_order_id"]]["status"] == "PARTIALLY_FILLED"

    engaged = client.post("/admin/kill-switch/engage")
    assert engaged.status_code == 200
    assert engaged.json()["cancelled_count"] == 5
    assert set(engaged.json()["cancelled"]) == set(cids)

    # Every order is CANCELLED in the store, each via a genuine PENDING_CANCEL →
    # CANCELLED transition (the real append-only audit mechanism; Design §6).
    for cid in cids:
        assert store.orders[cid]["status"] == "CANCELLED"
        states = [e.engine_state for e in hub._ring if e.client_order_id == cid]
        assert states[-2:] == ["PENDING_CANCEL", "CANCELLED"]

    rejected = client.post("/orders", json=order_payload(symbol="BLOCKED"))
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "kill_switch_engaged"

    assert client.post("/admin/kill-switch/disengage").status_code == 200
    accepted = client.post("/orders", json=order_payload(symbol="AFTER"))
    assert accepted.status_code == 201


# ------------------------------------------------------------- strategy id (D16)


def test_submit_with_strategy_id_header_reaches_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = _owner_client(monkeypatch)
    body = order_payload()
    response = client.post("/orders", json=body, headers={"X-Strategy-Id": "csm-set"})
    assert response.status_code == 201
    # The header is stamped onto the persisted order (and thus the stream).
    assert store.orders[body["client_order_id"]]["strategy_id"] == "csm-set"


def test_submit_without_strategy_id_header_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = _owner_client(monkeypatch)
    body = order_payload()
    assert client.post("/orders", json=body).status_code == 201
    assert store.orders[body["client_order_id"]]["strategy_id"] is None


def test_submit_invalid_strategy_id_header_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, store, _ = _owner_client(monkeypatch)
    body = order_payload()
    # A space is outside the conservative slug charset.
    response = client.post("/orders", json=body, headers={"X-Strategy-Id": "bad id"})
    assert response.status_code == 422
    assert store.orders == {}  # rejected before any insert


def test_kill_switch_env_pinned_disengage_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, _ = _owner_client(monkeypatch, kill_switch_engaged=True)
    response = client.post("/admin/kill-switch/disengage")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "kill_switch_env_pinned"


def test_kill_switch_engage_without_redis_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    client, _ = build_client(settings=make_settings(public_mode=False), pool=object(), redis=None)
    response = client.post("/admin/kill-switch/engage")
    assert response.status_code == 503


def test_kill_switch_disengage_without_redis_is_409_not_engaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no redis, status() reports not-engaged ⇒ disengage is a clean 409."""
    store = MemStore()
    patch_repositories(monkeypatch, store)
    client, _ = build_client(settings=make_settings(public_mode=False), pool=object(), redis=None)
    response = client.post("/admin/kill-switch/disengage")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "kill_switch_not_engaged"

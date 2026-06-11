"""API surface: modes, auth, envelopes, lifecycle through HTTP."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from src.quant_execution_engine.config.settings import Settings

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
    assert body["cancelled"] == [resting["client_order_id"]]
    assert body["failed"] == []

    rejected = client.post("/orders", json=order_payload())
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "kill_switch_engaged"

    disengaged = client.post("/admin/kill-switch/disengage")
    assert disengaged.status_code == 200
    assert disengaged.json()["engaged"] is False
    assert client.post("/orders", json=order_payload(symbol="OK")).status_code == 201


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

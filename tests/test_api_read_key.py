"""🔑 The read-only engine key ([[TK-0442]] engine layer, GH #395, operator ruling 2026-09-29).

The operator's requirement, verbatim in spirit: the read key presented to ``POST
/admin/kill-switch/engage``, and to every order write, returns 403. That path is why this exists —
with the full key, a monitoring page could mass-cancel every open order in one call.

Three layers, because each one alone can pass for the wrong reason:

* **HTTP, with side effects checked** — a 403 proves nothing if the handler ran anyway, so the
  kill-switch test also asserts the switch is still disengaged and the resting order is still live.
* **Positive controls** — the SAME requests with the full key must get through the guard. Without
  that, "refused" could come from anything (PR #56's first tests passed with the guard deleted,
  because that bridge answers 401 for an unrelated reason).
* **Structural** — walk every mounted route and pin exactly which accept the read key. Streams are
  covered HERE ONLY: an SSE route that failed to reject would open and hang the suite.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from src.quant_execution_engine.api import deps
from src.quant_execution_engine.api.deps import (
    READ_KEY_ROUTES,
    require_api_key,
    require_read_or_full_key,
)
from src.quant_execution_engine.api.main import GATEWAY_PROXY_PREFIX, create_app

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import TEST_API_KEY, build_client, make_settings, order_payload

READ_KEY = "test-read-only-key"
FULL = {"X-API-Key": TEST_API_KEY}
READ = {"X-API-Key": READ_KEY}
PFX = GATEWAY_PROXY_PREFIX

# Every route the page must NEVER reach with the read key. Streams are deliberately absent here
# (see module docstring) and are covered by the structural walk instead.
ORDER_WRITES_AND_KILL_SWITCH: list[tuple[str, str, dict[str, Any] | None]] = [
    ("post", "/admin/kill-switch/engage", None),
    ("post", "/admin/kill-switch/disengage", None),
    ("post", "/orders", "ORDER"),  # type: ignore[list-item]
    ("patch", "/orders/SOME-CID", {"new_price": "10.00"}),
    ("delete", "/orders/SOME-CID", None),
    ("post", f"{PFX}/orders", "ORDER"),  # type: ignore[list-item]
    ("patch", f"{PFX}/orders/SOME-CID", {"new_price": "10.00"}),
    ("delete", f"{PFX}/orders/SOME-CID", None),
]
OTHER_GUARDED_READS = [
    "/orders/SOME-CID",
    "/capabilities",
    "/admin/kill-switch",
    "/admin/audit/export",
    "/admin/orders/SOME-CID/audit",
    "/order-book/PTT",
    f"{PFX}/orders/SOME-CID",
    f"{PFX}/capabilities",
]
ALLOWLIST_PATHS = [
    "/accounts/ACCT?broker=sim",
    "/accounts/ACCT/positions?broker=sim",
    "/accounts/ACCT/open-orders?broker=sim",
]


def _client(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> TestClient:
    """Owner mode, a read key configured, and NO default header — every request states its key."""
    patch_repositories(monkeypatch, MemStore())
    overrides.setdefault("read_api_key", READ_KEY)
    settings = make_settings(public_mode=False, **overrides)
    client, _ = build_client(
        settings=settings, pool=object(), redis=FakeRedis(), send_api_key=False
    )
    return client


def _send(client: TestClient, method: str, path: str, body: Any, headers: dict[str, str]) -> Any:
    if body == "ORDER":
        body = order_payload()
    if method in ("post", "patch") and body is not None:
        return getattr(client, method)(path, json=body, headers=headers)
    return getattr(client, method)(path, headers=headers)


def _code(resp: Any) -> str | None:
    try:
        err = resp.json().get("error")
    except Exception:  # noqa: BLE001 - a non-JSON body simply has no code
        return None
    return err.get("code") if isinstance(err, dict) else None


# --------------------------------------------------------------- the operator's requirement


@pytest.mark.parametrize(("method", "path", "body"), ORDER_WRITES_AND_KILL_SWITCH)
def test_read_key_gets_a_typed_403_on_every_order_write_and_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
) -> None:
    resp = _send(_client(monkeypatch), method, path, body, READ)
    assert resp.status_code == 403, (path, resp.status_code, resp.text[:200])
    # The CODE is the discriminator: a 403 from public mode or the stage ladder is not this.
    assert _code(resp) == "read_key_forbidden", (path, resp.text[:200])


def test_read_key_engage_has_NO_side_effect_and_the_full_key_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 A 403 is not enough: the switch must still be off and the resting order still live."""
    client = _client(monkeypatch)
    resting = order_payload(metadata={"sim_fills": []})
    assert client.post("/orders", json=resting, headers=FULL).status_code == 201
    cid = resting["client_order_id"]

    refused = client.post("/admin/kill-switch/engage", headers=READ)
    assert refused.status_code == 403 and _code(refused) == "read_key_forbidden"
    assert client.get("/admin/kill-switch", headers=FULL).json()["engaged"] is False
    assert client.get(f"/orders/{cid}", headers=FULL).json()["engine_state"] != "CANCELLED"

    # Positive control: the SAME request with the full key goes through and mass-cancels.
    engaged = client.post("/admin/kill-switch/engage", headers=FULL)
    assert engaged.status_code == 200 and engaged.json()["engaged"] is True
    assert engaged.json()["cancelled"] == [cid]


@pytest.mark.parametrize(("method", "path", "body"), ORDER_WRITES_AND_KILL_SWITCH)
def test_full_key_is_never_refused_as_read_key_on_the_same_requests(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any
) -> None:
    """Positive control for every row above: the full key's holders keep working unchanged."""
    resp = _send(_client(monkeypatch), method, path, body, FULL)
    assert resp.status_code not in (401, 503), (path, resp.status_code)
    assert _code(resp) != "read_key_forbidden", (path, resp.text[:200])


@pytest.mark.parametrize("path", OTHER_GUARDED_READS)
def test_read_key_is_refused_on_every_guarded_route_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    resp = _client(monkeypatch).get(path, headers=READ)
    assert resp.status_code == 403 and _code(resp) == "read_key_forbidden", (path, resp.text[:200])


@pytest.mark.parametrize("path", ALLOWLIST_PATHS + [f"{PFX}{p}" for p in ALLOWLIST_PATHS])
def test_read_key_passes_the_guard_on_the_account_reads(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """At the default ``sim`` stage the read is answered, which proves the guard let it through."""
    resp = _client(monkeypatch).get(path, headers=READ)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])


# --------------------------------------------------------------- what must NOT change


def test_unset_read_key_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """It ADDS a key and revokes none: with no read key configured, nothing changes."""
    client = _client(monkeypatch, read_api_key=None)
    assert client.get("/accounts/ACCT?broker=sim", headers=FULL).status_code == 200
    assert client.post("/orders", json=order_payload(), headers=FULL).status_code == 201
    stray = client.post("/admin/kill-switch/engage", headers=READ)
    assert stray.status_code == 401, "an unconfigured read key is just a wrong key"
    assert client.get("/accounts/ACCT?broker=sim", headers=READ).status_code == 401


def test_wrong_key_is_401_everywhere_not_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    wrong = {"X-API-Key": "not-a-key"}
    assert client.get("/accounts/ACCT?broker=sim", headers=wrong).status_code == 401
    assert client.post("/admin/kill-switch/engage", headers=wrong).status_code == 401


def test_no_full_key_configured_fails_closed_even_for_the_read_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, api_key=None)
    assert client.get("/accounts/ACCT?broker=sim", headers=READ).status_code == 503


def test_a_read_key_equal_to_the_full_key_is_never_treated_as_scoped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(read_api_key=TEST_API_KEY)
    with caplog.at_level(logging.ERROR):
        assert deps._is_read_key(TEST_API_KEY, settings) is False
    assert any("NOT a scoped key" in r.message for r in caplog.records)


# --------------------------------------------------------------- structural pin


def _dependency_calls(route: APIRoute) -> set[Any]:
    seen: set[Any] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            seen.add(dep.call)
        stack.extend(dep.dependencies)
    return seen


def _all_routes() -> list[APIRoute]:
    return [r for r in create_app().routes if isinstance(r, APIRoute)]


def test_structural_the_read_key_is_accepted_on_EXACTLY_the_allowlist() -> None:
    routes = _all_routes()
    # 🔴 Positive control on the walk itself: an empty walk would make every assertion below
    # vacuously true (the bridge's app exposed six top-level entries and none of its surface).
    assert len(routes) >= 25, f"walked only {len(routes)} routes — the walk is broken, not the app"

    accepting: set[tuple[str, str]] = set()
    unauthenticated: set[tuple[str, str]] = set()
    for route in routes:
        calls = _dependency_calls(route)
        for method in route.methods or ():
            native = route.path[len(PFX) :] if route.path.startswith(PFX) else route.path
            if require_read_or_full_key in calls:
                assert require_api_key not in calls, f"{method} {route.path} carries both guards"
                accepting.add((method, native))
            elif require_api_key not in calls:
                unauthenticated.add((method, route.path))

    assert accepting == set(READ_KEY_ROUTES), sorted(accepting ^ set(READ_KEY_ROUTES))
    # A NEW unguarded route must be a decision, not an accident.
    assert unauthenticated == {("GET", "/health"), ("GET", f"{PFX}/health")}, sorted(
        unauthenticated
    )


def test_structural_streams_refuse_the_read_key() -> None:
    """Covered structurally because an SSE route that failed to reject would hang the suite."""
    streams = [r for r in _all_routes() if r.path.endswith("/stream")]
    assert len(streams) >= 4, [r.path for r in streams]
    for route in streams:
        calls = _dependency_calls(route)
        assert require_api_key in calls and require_read_or_full_key not in calls, route.path

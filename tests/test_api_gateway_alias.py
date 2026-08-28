"""The gateway proxy surface served directly by the engine (EH3 addendum).

`quant-api-gateway` proxies this service under ``/api/v2/engines/execution/*``.
On the AWS execution host there is no gateway container, and a strategy's order
path is hardcoded to that prefix, so the engine serves it too.

Every test here carries a **positive control**: an assertion that the thing being
excluded/rejected is genuinely present/accepted somewhere else in the same app.
Without one, "``/admin`` is absent under the alias" is satisfied by an app that
has no routes at all.
"""

from __future__ import annotations

from fastapi.routing import APIRoute
from src.quant_execution_engine.api.main import (
    ALIAS_EXCLUDED_PREFIX,
    GATEWAY_PROXY_PREFIX,
    create_app,
)

from tests.conftest import build_client, make_settings, order_payload


def _routes(app: object) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:  # type: ignore[attr-defined]
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                out.add((method, route.path))
    return out


def _is_admin(path: str) -> bool:
    return path == ALIAS_EXCLUDED_PREFIX or path.startswith(f"{ALIAS_EXCLUDED_PREFIX}/")


def test_alias_mirrors_every_non_admin_native_route() -> None:
    """The alias is DERIVED, so it must equal the native surface minus /admin."""
    routes = _routes(create_app())
    native = {(m, p) for m, p in routes if not p.startswith(GATEWAY_PROXY_PREFIX)}
    aliased = {(m, p) for m, p in routes if p.startswith(GATEWAY_PROXY_PREFIX)}

    expected = {(m, f"{GATEWAY_PROXY_PREFIX}{p}") for m, p in native if not _is_admin(p)}
    assert aliased == expected

    # Positive control: the comparison above is vacuous if either side is empty.
    # Anti-vacuity control only — the REAL assertion is the set equality above, which
    # passed unchanged when /accounts/{account} and /accounts/{account}/open-orders were
    # added (the alias is derived, so it mirrored them automatically). 9 -> 11 for those two,
    # and 11 -> 12 when GET /accounts/{account}/positions landed 2026-08-28 — again without
    # touching this file's production code, which is the property being asserted.
    assert len(aliased) == 12, f"expected the 12 gateway-proxied routes, got {len(aliased)}"


def test_admin_is_absent_under_the_alias_but_present_natively() -> None:
    """The gateway never proxied /admin (kill-switch); the alias must not widen it.

    Deliberately spelled ``"/admin"`` literally rather than via
    ``ALIAS_EXCLUDED_PREFIX``: the constant is the thing under test, so a test
    that imported it would simply move with a wrong value instead of catching it.
    """
    routes = _routes(create_app())

    leaked = [p for _, p in routes if p.startswith(GATEWAY_PROXY_PREFIX) and "/admin" in p]
    assert leaked == []

    # Positive control: /admin really does exist on this app, so the emptiness
    # above is an exclusion rather than an app with no admin surface at all.
    native_admin = [p for _, p in routes if p.startswith("/admin")]
    assert "/admin/kill-switch/engage" in native_admin
    assert "/admin/audit/export" in native_admin


def test_alias_orders_stream_does_not_shadow_get_order() -> None:
    """Literal /orders/stream must out-rank /orders/{cid} under the alias too.

    FastAPI matches in registration order, so the aliased router has to preserve
    the streams-before-core ordering the native mount already depends on.
    """
    paths = [
        r.path
        for r in create_app().routes
        if isinstance(r, APIRoute) and r.path.startswith(GATEWAY_PROXY_PREFIX)
    ]
    stream = paths.index(f"{GATEWAY_PROXY_PREFIX}/orders/stream")
    by_id = paths.index(f"{GATEWAY_PROXY_PREFIX}/orders/{{client_order_id}}")
    assert stream < by_id


def test_health_is_identical_on_both_path_shapes() -> None:
    """Behavioural parity, not merely route-table parity."""
    client, _ = build_client(settings=make_settings(public_mode=False))
    native = client.get("/health")
    aliased = client.get(f"{GATEWAY_PROXY_PREFIX}/health")

    assert native.status_code == aliased.status_code == 200
    assert native.json() == aliased.json()


def test_alias_preserves_the_owner_mode_guard() -> None:
    """Re-mounting must not strip route dependencies.

    This is the failure that would matter: an alias that answered while the
    native path refused would be a hole in the credential boundary, not a
    convenience.
    """
    client, _ = build_client(settings=make_settings(public_mode=True))

    aliased = client.post(f"{GATEWAY_PROXY_PREFIX}/orders", json=order_payload())
    assert aliased.status_code == 403
    assert aliased.json()["error"]["code"] == "public_mode"

    # Positive control: the native path refuses identically, so the 403 above is
    # the guard firing rather than the alias being broken/absent.
    native = client.post("/orders", json=order_payload())
    assert native.status_code == 403
    assert native.json()["error"]["code"] == "public_mode"


def test_alias_preserves_the_api_key_guard() -> None:
    """A configured API key is enforced on the alias exactly as natively."""
    settings = make_settings(public_mode=False, api_key="s3cret")
    client, _ = build_client(settings=settings, pool=object(), redis=None)

    assert client.get(f"{GATEWAY_PROXY_PREFIX}/capabilities").status_code == 401
    assert client.get("/capabilities").status_code == 401

    # Positive control: with the key present the same route answers, so the 401s
    # above are the guard and not an unroutable path.
    ok = client.get(f"{GATEWAY_PROXY_PREFIX}/capabilities", headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200

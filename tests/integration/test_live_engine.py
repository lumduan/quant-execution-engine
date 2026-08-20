"""Live-stack smoke tests (run with: uv run pytest -m integration --no-cov).

Requires the engine container up on quant-network (host :8400 by default;
override with EXECUTION_ENGINE_TEST_BASE_URL). The full owner-mode lifecycle
acceptance (submit/dedupe/partial fills/cancel/kill-switch + db_execution
audit rows) is exercised by the Phase 2 verification runbook in
docs/plans/phase2-engine-core-simadapter.md.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("EXECUTION_ENGINE_TEST_BASE_URL", "http://localhost:8400")


async def test_health_answers() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "quant-execution-engine"


async def test_capabilities_answers() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        response = await client.get("/capabilities")
    assert response.status_code == 200
    brokers = {row["broker"] for row in response.json()["capabilities"]}
    assert {"sim", "liberator", "streaming_pro"} <= brokers


async def test_public_mode_blocks_submits_by_default() -> None:
    """The Docker default is public mode — submits must 403 until owner-mode."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        health = await client.get("/health")
        if not health.json().get("public_mode", True):
            pytest.skip("engine is in owner mode on this host")
        response = await client.post("/orders", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "public_mode"


# ---------------------------------------------------------------------------
# The gateway proxy surface, served directly (feature-execution-ha / EH3).
#
# These are the checks that make the AWS stand-up verifiable against the RUNNING
# service rather than against its source. On the AWS execution host there is no
# quant-api-gateway, so this prefix IS the strategy's order path:
#
#   EXECUTION_ENGINE_TEST_BASE_URL=http://<node>:8400 \
#       uv run pytest -m integration --no-cov
# ---------------------------------------------------------------------------

# Spelled literally, not imported from the app: these tests assert what a REMOTE
# caller must find at a fixed URL. Importing the constant would make the test
# agree with the code by construction even if the published path changed.
GATEWAY_PROXY_PREFIX = "/api/v2/engines/execution"


async def test_alias_health_matches_native_on_the_live_service() -> None:
    """Same service, same answer, both path shapes."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        native = await client.get("/health")
        aliased = await client.get(f"{GATEWAY_PROXY_PREFIX}/health")

    assert native.status_code == aliased.status_code == 200
    assert native.json() == aliased.json()


async def test_alias_admin_is_not_reachable_on_the_live_service() -> None:
    """The kill-switch surface must NOT be reachable through the alias.

    The discriminator is 404-vs-not-404, not 'refused'. Natively /admin answers
    401/403 (api-key + owner-mode) — a refusal, which proves the route EXISTS.
    Under the alias it must be a 404: no such route at all. Asserting only
    "the alias refuses" would be satisfied by the route existing and merely
    being guarded, which is the thing this is meant to rule out.
    """
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        aliased = await client.get(f"{GATEWAY_PROXY_PREFIX}/admin/kill-switch")
        native = await client.get("/admin/kill-switch")

    assert aliased.status_code == 404
    # Positive control: the route is genuinely served natively, so the 404 above
    # is an exclusion rather than a service that has no admin surface at all.
    assert native.status_code != 404

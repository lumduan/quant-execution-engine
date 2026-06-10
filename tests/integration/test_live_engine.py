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
    assert {"sim", "liberator", "settrade"} <= brokers


async def test_public_mode_blocks_submits_by_default() -> None:
    """The Docker default is public mode — submits must 403 until owner-mode."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        health = await client.get("/health")
        if not health.json().get("public_mode", True):
            pytest.skip("engine is in owner mode on this host")
        response = await client.post("/orders", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "public_mode"

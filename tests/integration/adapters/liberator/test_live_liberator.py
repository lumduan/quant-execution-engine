"""Live-stack checks against the bundled liberator overlay (owner mode).

Requires the full overlay up on quant-network:

    docker compose -f docker-compose.yml -f docker-compose.private.yml \
                   -f docker-compose.liberator.yml up -d

Run explicitly (excluded by default): ``uv run pytest -m integration --no-cov``.
No credentials are asserted here — only reachability and contract shapes; the
real OTP login + micro_live order is the operator runbook in
``.claude/playbooks/order-routing-safety.md``.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

_ENGINE = os.environ.get("EXECUTION_ENGINE_URL", "http://localhost:8400")


async def test_engine_health_exposes_liberator_breaker_state() -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{_ENGINE}/health")
    assert response.status_code == 200
    body = response.json()
    brokers = body.get("brokers")
    if brokers is None:
        pytest.skip("liberator runtime not configured (sim/public bring-up)")
    assert brokers["liberator"]["breaker_state"] in ("closed", "open", "half_open")


async def test_capabilities_show_liberator_installed() -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{_ENGINE}/capabilities")
    assert response.status_code == 200
    rows = [c for c in response.json()["capabilities"] if c["broker"] == "liberator"]
    assert rows and all(c["adapter_installed"] for c in rows)
    assert all(c["amend"] == "cancel_replace" for c in rows)

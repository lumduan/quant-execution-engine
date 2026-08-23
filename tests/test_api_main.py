"""App factory + lifespan: resilient startup, health under degradation."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from src.quant_execution_engine.api import main as api_main
from src.quant_execution_engine.db.errors import RepositoryError

from tests._fakes import FakeRedis
from tests.conftest import make_settings


def _patch_lifespan_deps(monkeypatch: pytest.MonkeyPatch, *, pool_fails: bool) -> dict[str, int]:
    calls = {"pool": 0, "redis": 0, "closed_pool": 0, "closed_redis": 0}

    async def fake_create_pool(dsn: str, *, min_size: int, max_size: int) -> Any:
        calls["pool"] += 1
        if pool_fails:
            raise RepositoryError("db down")
        return object()

    def fake_create_redis(url: str) -> Any:
        calls["redis"] += 1
        return FakeRedis()

    async def fake_close_pool() -> None:
        calls["closed_pool"] += 1

    async def fake_close_redis() -> None:
        calls["closed_redis"] += 1

    monkeypatch.setattr(api_main, "get_settings", lambda: make_settings())
    monkeypatch.setattr(api_main, "create_pool", fake_create_pool)
    monkeypatch.setattr(api_main, "create_redis", fake_create_redis)
    monkeypatch.setattr(api_main, "close_pool", fake_close_pool)
    monkeypatch.setattr(api_main, "close_redis", fake_close_redis)
    return calls


def test_lifespan_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic: `create_app()` builds REAL Settings, which read the host `.env`. Without this
    # the assertion below tests the developer's machine rather than the code — it went red the
    # moment HOME's `.env` gained EXECUTION_ENGINE_STAGE=paper (2026-08-23), while CI stayed
    # green because CI has no `.env`. An env var beats the `.env` file, so pinning it here makes
    # the test independent of host config without changing what it asserts.
    monkeypatch.setenv("EXECUTION_ENGINE_STAGE", "sim")
    calls = _patch_lifespan_deps(monkeypatch, pool_fails=False)
    with TestClient(api_main.create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "quant-execution-engine"
        assert body["stage"] == "sim"
    assert calls == {"pool": 1, "redis": 1, "closed_pool": 1, "closed_redis": 1}


def test_lifespan_degrades_without_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_lifespan_deps(monkeypatch, pool_fails=True)
    with TestClient(api_main.create_app()) as client:
        assert client.get("/health").status_code == 200  # probe surface survives
    assert calls["closed_pool"] == 1 and calls["closed_redis"] == 1


def test_module_level_app_exists() -> None:
    assert api_main.app.title == "quant-execution-engine"

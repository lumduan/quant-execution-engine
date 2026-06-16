"""Shared fixtures: singleton resets, settings factory, app builder."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.quant_execution_engine.adapters import market_data as market_data_module
from src.quant_execution_engine.adapters import sim_pricing
from src.quant_execution_engine.adapters.liberator import runtime as liberator_runtime
from src.quant_execution_engine.adapters.settrade import runtime as settrade_runtime
from src.quant_execution_engine.adapters.streaming_pro import runtime as streaming_pro_runtime
from src.quant_execution_engine.api import deps
from src.quant_execution_engine.api.main import create_app
from src.quant_execution_engine.cache import redis_client as redis_module
from src.quant_execution_engine.config.settings import Settings, get_settings
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.db import postgres as postgres_module
from src.quant_execution_engine.events.hub import reset_event_hub
from src.quant_execution_engine.order_book import runtime as order_book_runtime


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: requires a live quant-network stack (skipped by default)"
    )


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Keep module-level singletons and the settings cache test-hermetic."""
    get_settings.cache_clear()
    yield
    postgres_module._pool = None
    redis_module._client = None
    for task in liberator_runtime._tasks:
        task.cancel()
    liberator_runtime._tasks.clear()
    liberator_runtime._adapter = None
    for task in settrade_runtime._tasks:
        task.cancel()
    settrade_runtime._tasks.clear()
    settrade_runtime._adapter = None
    for task in streaming_pro_runtime._tasks:
        task.cancel()
    streaming_pro_runtime._tasks.clear()
    streaming_pro_runtime._adapter = None
    for task in order_book_runtime._tasks:
        task.cancel()
    order_book_runtime._tasks.clear()
    order_book_runtime._service = None
    order_book_runtime._router = None
    sim_pricing._pricer = None
    market_data_module._client = None
    reset_event_hub()
    get_settings.cache_clear()


def make_settings(**overrides: Any) -> Settings:
    """Settings detached from .env and the process environment."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def make_order(**overrides: Any) -> NormalizedOrder:
    """A valid LIMIT SET order with a fresh UUIDv4; override any field."""
    payload: dict[str, Any] = {
        "client_order_id": str(uuid4()),
        "broker": "sim",
        "account": "ACC-TEST",
        "market": "SET",
        "symbol": "PTT",
        "side": "BUY",
        "order_type": "LIMIT",
        "price": "123.456789",
        "quantity": 100,
        "tif": "DAY",
    }
    payload.update(overrides)
    return NormalizedOrder(**payload)


def order_payload(**overrides: Any) -> dict[str, Any]:
    """A valid POST /orders JSON body; override any field."""
    payload: dict[str, Any] = {
        "client_order_id": str(uuid4()),
        "broker": "sim",
        "account": "ACC-TEST",
        "market": "SET",
        "symbol": "PTT",
        "side": "BUY",
        "order_type": "LIMIT",
        "price": "123.456789",
        "quantity": 100,
        "tif": "DAY",
    }
    payload.update(overrides)
    return payload


def build_client(
    *,
    settings: Settings,
    pool: Any = None,
    redis: Any = None,
) -> tuple[TestClient, FastAPI]:
    """App with dependency overrides; lifespan NOT run (no real pool/redis)."""
    app = create_app()
    app.dependency_overrides[deps.get_settings_dep] = lambda: settings
    app.dependency_overrides[deps.get_pool_dep] = lambda: pool
    app.dependency_overrides[deps.get_redis_dep] = lambda: redis
    return TestClient(app), app

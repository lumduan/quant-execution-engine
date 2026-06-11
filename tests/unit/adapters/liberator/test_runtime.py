"""Runtime singleton: start predicate matrix, worker selection, clean shutdown."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr
from src.quant_execution_engine.adapters.liberator import runtime
from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
from src.quant_execution_engine.contracts.enums import Market, OrderState

from tests._fakes import MemStore, patch_repositories
from tests.conftest import make_order, make_settings

_CREDS: dict[str, Any] = {
    "liberator_api_key": SecretStr("k"),
    "liberator_pin": SecretStr("123456"),
}


@pytest.mark.parametrize(
    ("stage", "public_mode", "with_creds", "expected"),
    [
        ("sim", False, True, False),  # sim never talks to a broker
        ("sim", True, True, False),
        ("paper", False, True, True),  # paper: live session for reads
        ("paper", True, True, False),  # public mode disables it
        ("paper", False, False, False),  # no creds, no runtime
        ("micro_live", False, True, True),
        ("micro_live", False, False, False),
        ("live", False, True, True),
        ("live", True, True, False),
    ],
)
def test_liberator_enabled_predicate(
    stage: str, public_mode: bool, with_creds: bool, expected: bool
) -> None:
    settings = make_settings(stage=stage, public_mode=public_mode, **(_CREDS if with_creds else {}))
    assert runtime.liberator_enabled(settings) is expected


def test_create_returns_none_when_disabled_and_warns_on_missing_creds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert runtime.create_liberator_runtime(make_settings(stage="sim")) is None
    assert runtime.get_liberator_adapter() is None
    with caplog.at_level("WARNING"):
        created = runtime.create_liberator_runtime(
            make_settings(stage="micro_live", public_mode=False)
        )
    assert created is None
    assert "credentials absent" in caplog.text


def test_create_is_a_singleton() -> None:
    settings = make_settings(stage="micro_live", public_mode=False, **_CREDS)
    first = runtime.create_liberator_runtime(settings)
    assert isinstance(first, LiberatorAdapter)
    assert runtime.create_liberator_runtime(settings) is first
    assert runtime.get_liberator_adapter() is first


async def test_resolver_reads_the_durable_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemStore()
    patch_repositories(monkeypatch, store)
    monkeypatch.setattr(runtime, "get_pool", lambda: object())
    order = make_order(broker="liberator", price="123.45")
    store.seed(order, OrderState.NEW)  # seeded rows carry broker_order_id B-SEED
    assert await runtime._resolve_order_from_store(order.client_order_id) == (
        "B-SEED",
        Market.SET,
    )
    pending = make_order(broker="liberator", price="123.45")
    store.seed(pending, OrderState.PENDING_NEW)  # no broker id yet
    assert await runtime._resolve_order_from_store(pending.client_order_id) is None
    assert await runtime._resolve_order_from_store("unknown") is None


async def _stub_loop(*args: Any, **kwargs: Any) -> None:
    await asyncio.sleep(3600)


class _StubReconciler:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def run(self) -> None:
        await asyncio.sleep(3600)


@pytest.mark.parametrize(
    ("stage", "expected_tasks"),
    [("paper", 1), ("micro_live", 2), ("live", 2)],
)
async def test_worker_selection_by_stage(
    monkeypatch: pytest.MonkeyPatch, stage: str, expected_tasks: int
) -> None:
    monkeypatch.setattr(runtime, "heartbeat_loop", _stub_loop)
    monkeypatch.setattr(runtime, "LiberatorReconciler", _StubReconciler)
    settings = make_settings(stage=stage, public_mode=False, **_CREDS)
    runtime.create_liberator_runtime(settings)
    await runtime.start_liberator_workers(settings)
    assert len(runtime._tasks) == expected_tasks
    await runtime.close_liberator_runtime()
    assert runtime._tasks == []
    assert runtime.get_liberator_adapter() is None
    await runtime.close_liberator_runtime()  # idempotent no-op


async def test_start_without_runtime_is_a_no_op() -> None:
    settings = make_settings(stage="sim")
    await runtime.start_liberator_workers(settings)
    assert runtime._tasks == []

"""`/capabilities` must report what THIS deployment can route, not what the build contains."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from src.quant_execution_engine.adapters.liberator import runtime as liberator_runtime
from src.quant_execution_engine.adapters.streaming_pro import runtime as sp_runtime
from src.quant_execution_engine.contracts.capabilities import CAPABILITY_MATRIX

from tests._fakes import FakeRedis, StubBrokerAdapter
from tests.conftest import build_client, make_settings


def _stub() -> StubBrokerAdapter:
    """A stub the sibling `brokers` field can also read.

    `_broker_runtime_health()` reads `last_heartbeat_ok`, which StubBrokerAdapter does not
    define. Setting it here keeps this file's subject (`adapter_installed`) isolated from an
    unrelated fixture gap.
    """
    a = StubBrokerAdapter()
    a.last_heartbeat_ok = True  # type: ignore[attr-defined]
    return a


def _installed(client: TestClient) -> dict[str, bool]:
    body = client.get("/capabilities", headers={"X-API-Key": "k"}).json()
    return {c["broker"]: c["adapter_installed"] for c in body["capabilities"]}


def test_a_broker_with_NO_runtime_reports_adapter_installed_FALSE() -> None:
    """🔴 The bug this replaces, reproduced as a test.

    `adapter_installed` was served straight from CAPABILITY_MATRIX, where it is a hardcoded
    True. On the AWS node that produced `liberator adapter_installed=True` on a node holding
    no Liberator credential — a micro_live order there is StageRejected, not a fill.
    session:cash-carry hit it while planning a gate, and broker-commands.md §6 tells authors to
    query this endpoint rather than hardcode, so the documented advice led into it.
    """
    client, _ = build_client(settings=make_settings(api_key="k"), pool=object(), redis=FakeRedis())
    installed = _installed(client)
    assert installed["liberator"] is False
    assert installed["streaming_pro"] is False
    assert installed["sim"] is True, "sim needs no credential and is always constructed"


def test_a_broker_WITH_a_runtime_reports_TRUE(monkeypatch: Any) -> None:
    """The positive control. Without it, 'reports False' is met by hardcoding False —
    which would be a different lie, not a fix."""
    monkeypatch.setattr(liberator_runtime, "_adapter", _stub())
    client, _ = build_client(settings=make_settings(api_key="k"), pool=object(), redis=FakeRedis())
    installed = _installed(client)
    assert installed["liberator"] is True
    assert installed["streaming_pro"] is False, "still absent — the two are independent"


def test_the_static_matrix_is_NOT_mutated_by_serving_it(monkeypatch: Any) -> None:
    """Rows are rebuilt per response with model_copy; the frozen contract must stay intact.

    If serving mutated CAPABILITY_MATRIX, the first request would permanently corrupt the
    matrix the ROUTER enforces — turning a display bug into an order-routing bug.
    """
    monkeypatch.setattr(sp_runtime, "_adapter", _stub())
    client, _ = build_client(settings=make_settings(api_key="k"), pool=object(), redis=FakeRedis())
    _installed(client)
    assert all(row.adapter_installed is True for row in CAPABILITY_MATRIX), (
        "CAPABILITY_MATRIX was mutated by serving it"
    )


def test_order_type_cells_are_served_unchanged() -> None:
    """Only adapter_installed is recomputed — the contract cells must pass through verbatim."""
    client, _ = build_client(settings=make_settings(api_key="k"), pool=object(), redis=FakeRedis())
    body = client.get("/capabilities", headers={"X-API-Key": "k"}).json()
    served = {(c["broker"], c["market"]): tuple(c["order_types"]) for c in body["capabilities"]}
    for row in CAPABILITY_MATRIX:
        assert served[(row.broker.value, row.market.value)] == tuple(
            o.value for o in row.order_types
        )

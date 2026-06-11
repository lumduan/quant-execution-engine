"""The Phase 3/4 stage×broker×intent routing matrix (core/stage.py)."""

from __future__ import annotations

import pytest
from src.quant_execution_engine.adapters.sim import SimAdapter
from src.quant_execution_engine.contracts.enums import Broker, Stage
from src.quant_execution_engine.contracts.errors import StageRejected
from src.quant_execution_engine.core.stage import AdapterIntent, resolve_adapter

from tests._fakes import StubBrokerAdapter
from tests.conftest import make_settings
from tests.unit.adapters.liberator.test_adapter_place import make_adapter


def _sim() -> SimAdapter:
    return SimAdapter(default_fill_price=make_settings().sim_default_fill_price)


def _StubBrokerAdapter() -> StubBrokerAdapter:
    return StubBrokerAdapter(broker=Broker.SETTRADE)


def test_sim_stage_routes_every_broker_to_sim_even_when_liberator_exists() -> None:
    sim, liberator = _sim(), make_adapter()
    for broker in Broker:
        resolved = resolve_adapter(Stage.SIM, broker, sim_adapter=sim, liberator_adapter=liberator)
        assert resolved is sim  # sim is a stage, not a broker


def test_paper_trade_intent_is_intercepted_to_sim() -> None:
    sim, liberator = _sim(), make_adapter()
    resolved = resolve_adapter(
        Stage.PAPER,
        Broker.LIBERATOR,
        sim_adapter=sim,
        liberator_adapter=liberator,
        intent=AdapterIntent.TRADE,
    )
    assert resolved is sim  # placements never reach the venue at paper


def test_paper_read_intent_reaches_liberator_when_configured() -> None:
    sim, liberator = _sim(), make_adapter()
    resolved = resolve_adapter(
        Stage.PAPER,
        Broker.LIBERATOR,
        sim_adapter=sim,
        liberator_adapter=liberator,
        intent=AdapterIntent.READ,
    )
    assert resolved is liberator  # account/position realism
    # Without a configured runtime, reads degrade to sim (never crash).
    degraded = resolve_adapter(
        Stage.PAPER, Broker.LIBERATOR, sim_adapter=sim, intent=AdapterIntent.READ
    )
    assert degraded is sim
    # Non-liberator brokers always read from sim at paper.
    other = resolve_adapter(
        Stage.PAPER,
        Broker.SIM,
        sim_adapter=sim,
        liberator_adapter=liberator,
        intent=AdapterIntent.READ,
    )
    assert other is sim


def test_micro_live_routes_liberator_and_rejects_everything_else() -> None:
    sim, liberator = _sim(), make_adapter()
    resolved = resolve_adapter(
        Stage.MICRO_LIVE, Broker.LIBERATOR, sim_adapter=sim, liberator_adapter=liberator
    )
    assert resolved is liberator
    with pytest.raises(StageRejected, match="configured liberator runtime"):
        resolve_adapter(Stage.MICRO_LIVE, Broker.LIBERATOR, sim_adapter=sim)
    # broker=sim still has no real micro_live adapter.
    with pytest.raises(StageRejected, match="no installed adapter"):
        resolve_adapter(Stage.MICRO_LIVE, Broker.SIM, sim_adapter=sim, liberator_adapter=liberator)


def test_micro_live_settrade_requires_a_configured_runtime() -> None:
    sim = _sim()
    stub = _StubBrokerAdapter()
    # With a configured settrade runtime, micro_live routes broker=settrade to it.
    resolved = resolve_adapter(
        Stage.MICRO_LIVE, Broker.SETTRADE, sim_adapter=sim, settrade_adapter=stub
    )
    assert resolved is stub
    # Without one, the ladder rejects with a settrade-specific runtime message.
    with pytest.raises(StageRejected, match="settrade runtime"):
        resolve_adapter(Stage.MICRO_LIVE, Broker.SETTRADE, sim_adapter=sim)


def test_paper_read_settrade_reaches_runtime_trade_intercepts() -> None:
    sim = _sim()
    stub = _StubBrokerAdapter()
    read = resolve_adapter(
        Stage.PAPER,
        Broker.SETTRADE,
        sim_adapter=sim,
        settrade_adapter=stub,
        intent=AdapterIntent.READ,
    )
    assert read is stub  # account/position realism
    trade = resolve_adapter(
        Stage.PAPER,
        Broker.SETTRADE,
        sim_adapter=sim,
        settrade_adapter=stub,
        intent=AdapterIntent.TRADE,
    )
    assert trade is sim  # placements never reach the venue at paper


def test_live_stays_gated_in_phase_4() -> None:
    sim, liberator = _sim(), make_adapter()
    for broker in Broker:
        with pytest.raises(StageRejected, match="gated"):
            resolve_adapter(Stage.LIVE, broker, sim_adapter=sim, liberator_adapter=liberator)

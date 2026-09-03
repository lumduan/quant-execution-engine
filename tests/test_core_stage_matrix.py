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

    # 🔴 CHANGED 2026-09-03 ([[TK-0488]]). This block asserted the OPPOSITE, with the comment
    # "Without a configured runtime, reads degrade to sim (never crash)." That was not an
    # oversight — it was a written design choice, which is exactly why it survived: "never
    # crash" reads as prudent until you notice what it substitutes.
    #
    # On a node with no credentials it made `GET /accounts/<anything>` answer 200 with SIM
    # DATA UNDER THE REAL BROKER'S NAME — measured on HOME against `00000000`, the declared
    # sentinel for an account the venue REFUSES: empty positions, empty open-orders, and a
    # fabricated `buying_power` of 1000000000. A crash is loud; a confident wrong number is
    # not, and a caller sizes against it.
    with pytest.raises(StageRejected, match="will NOT be faked"):
        resolve_adapter(Stage.PAPER, Broker.LIBERATOR, sim_adapter=sim, intent=AdapterIntent.READ)

    # ⚠️ And the case that keeps the fix from over-reaching: `broker=sim` must STILL read
    # from sim. `_real_adapter_for(SIM)` also returns None, so a fix gated on `real is None`
    # rather than on `_REAL_BROKERS` would raise here too — which is precisely the trap this
    # fix's own written proposal contained before it was implemented.
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


def test_micro_live_streaming_pro_requires_a_configured_runtime() -> None:
    sim = _sim()
    stub = StubBrokerAdapter(broker=Broker.STREAMING_PRO)
    resolved = resolve_adapter(
        Stage.MICRO_LIVE, Broker.STREAMING_PRO, sim_adapter=sim, streaming_pro_adapter=stub
    )
    assert resolved is stub
    with pytest.raises(StageRejected, match="streaming_pro runtime"):
        resolve_adapter(Stage.MICRO_LIVE, Broker.STREAMING_PRO, sim_adapter=sim)


def test_paper_streaming_pro_read_reaches_runtime_trade_intercepts() -> None:
    sim = _sim()
    stub = StubBrokerAdapter(broker=Broker.STREAMING_PRO)
    read = resolve_adapter(
        Stage.PAPER,
        Broker.STREAMING_PRO,
        sim_adapter=sim,
        streaming_pro_adapter=stub,
        intent=AdapterIntent.READ,
    )
    assert read is stub
    trade = resolve_adapter(
        Stage.PAPER,
        Broker.STREAMING_PRO,
        sim_adapter=sim,
        streaming_pro_adapter=stub,
        intent=AdapterIntent.TRADE,
    )
    assert trade is sim  # placements never reach the venue at paper


def test_live_stays_gated_in_phase_4() -> None:
    sim, liberator = _sim(), make_adapter()
    for broker in Broker:
        with pytest.raises(StageRejected, match="gated"):
            resolve_adapter(Stage.LIVE, broker, sim_adapter=sim, liberator_adapter=liberator)


# ------------------------------------------- TK-0488: a paper read must never be faked


def test_an_unanswerable_paper_read_RAISES_for_every_real_broker() -> None:
    """Both real brokers, not just the one that was measured.

    The defect was in the shared ladder, so it applied to `streaming_pro` identically —
    HOME returned `200 {"positions": []}` for it too. Fixing only the broker that happened
    to be in the reproduction would leave the other half live.
    """
    sim = _sim()
    for broker in (Broker.LIBERATOR, Broker.STREAMING_PRO):
        with pytest.raises(StageRejected, match="will NOT be faked"):
            resolve_adapter(Stage.PAPER, broker, sim_adapter=sim, intent=AdapterIntent.READ)


def test_a_paper_read_still_WORKS_when_the_runtime_IS_configured() -> None:
    """Positive control — "always raise" would pass the test above and break every paper read.

    A node WITH credentials must still reach the real venue at `paper`; that is the
    documented behaviour reads rely on, and it is what makes a paper balance read a live
    call rather than a rehearsal.
    """
    sim, liberator = _sim(), make_adapter()
    resolved = resolve_adapter(
        Stage.PAPER,
        Broker.LIBERATOR,
        sim_adapter=sim,
        liberator_adapter=liberator,
        intent=AdapterIntent.READ,
    )
    assert resolved is liberator


def test_a_paper_PLACEMENT_still_intercepts_to_sim_even_with_no_runtime() -> None:
    """🔑 The fix must touch READS ONLY. The paper intercept is the whole point of `paper`.

    If this ever raises, `paper` has stopped being a safe rehearsal stage — which would be a
    far worse regression than the bug being fixed, and it is the kind a reads-focused change
    could cause without anyone looking.
    """
    sim = _sim()
    for broker in (Broker.LIBERATOR, Broker.STREAMING_PRO, Broker.SIM):
        assert (
            resolve_adapter(Stage.PAPER, broker, sim_adapter=sim, intent=AdapterIntent.TRADE) is sim
        )


def test_sim_reads_are_UNAFFECTED_at_paper() -> None:
    """The over-reach guard, asserted on its own rather than only inside another test.

    `_real_adapter_for(SIM)` returns None, so a fix gated on `real is None` would raise here.
    Gating on `_REAL_BROKERS` is what keeps a legitimate sim read working.
    """
    sim = _sim()
    assert (
        resolve_adapter(Stage.PAPER, Broker.SIM, sim_adapter=sim, intent=AdapterIntent.READ) is sim
    )


def test_the_refusal_names_the_check_that_diagnoses_it() -> None:
    """A refusal that does not say how to confirm it just moves the confusion downstream.

    The operator's next question is always "is that this node, or is the venue down?" —
    `adapter_installed` on /capabilities answers it, so the message carries it.
    """
    sim = _sim()
    with pytest.raises(StageRejected) as exc:
        resolve_adapter(Stage.PAPER, Broker.LIBERATOR, sim_adapter=sim, intent=AdapterIntent.READ)
    msg = str(exc.value)
    assert "adapter_installed" in msg and "liberator" in msg

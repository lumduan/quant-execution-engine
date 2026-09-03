"""The ``EXECUTION_ENGINE_STAGE`` safety ladder (E2) — the route gate.

Stage matrix (decision log):

* ``sim``        — every broker routes to the deterministic ``SimAdapter``
  (sim is a STAGE, not a broker: the broker field is deliberately ignored).
* ``paper``      — TRADE intent is intercepted to sim (no real orders); READ
  intent for a real broker (``liberator``/``streaming_pro``) reaches the live
  broker session when one is configured (account/position realism).
* ``micro_live`` — a real broker (``liberator``/``streaming_pro``) routes to its
  adapter (PTRM caps enforce smallest size); every other broker is rejected.
* ``live``       — stays gated; always rejected.

Each real broker is wired symmetrically: it is reachable only when its runtime
is configured (owner mode + creds), else paper READ degrades to sim and
micro_live raises :class:`StageRejected`.
"""

from __future__ import annotations

from enum import StrEnum

from src.quant_execution_engine.adapters.base import BrokerAdapter
from src.quant_execution_engine.contracts.enums import Broker, Stage
from src.quant_execution_engine.contracts.errors import StageRejected


class AdapterIntent(StrEnum):
    """What the caller wants the adapter for — paper treats them differently."""

    TRADE = "trade"  # place / cancel / amend
    READ = "read"  # positions / account / venue open orders


_REAL_BROKERS = (Broker.LIBERATOR, Broker.STREAMING_PRO)


def resolve_adapter(
    stage: Stage,
    broker: Broker,
    *,
    sim_adapter: BrokerAdapter,
    liberator_adapter: BrokerAdapter | None = None,
    streaming_pro_adapter: BrokerAdapter | None = None,
    intent: AdapterIntent = AdapterIntent.TRADE,
) -> BrokerAdapter:
    """Return the adapter the ladder permits, or raise :class:`StageRejected`."""
    real = _real_adapter_for(broker, liberator_adapter, streaming_pro_adapter)
    if stage is Stage.SIM:
        return sim_adapter
    if stage is Stage.PAPER:
        # 🔴 A READ FOR A REAL BROKER MUST NEVER BE ANSWERED BY THE SIM ADAPTER.
        # This branch used to read `intent is READ and real is not None`, so a node with no
        # credentials — `adapter_installed=false` — fell through to `return sim_adapter` and
        # answered broker reads with SIM DATA UNDER THE REAL BROKER'S NAME. Measured on HOME
        # 2026-09-01 against `00000000`, the declared sentinel for an account the venue
        # REFUSES: `200 {"positions": []}`, `200 {"orders": []}`, and a fabricated
        # `buying_power: 1000000000` ([[TK-0488]]).
        #
        # `[]` is at least SHAPED like an absence. A billion baht of buying power is shaped
        # like a MEASUREMENT — a sizing calculation would not fail, it would size against
        # fiction. Three lines below, `micro_live` already raises on this exact condition;
        # the two stages disagreed about what an unanswerable read should do and only one of
        # them was safe.
        #
        # ⚠️ Gated on `_REAL_BROKERS`, NOT on `real is None`. `_real_adapter_for(sim)` also
        # returns None, so raising on that alone would break legitimate `broker=sim` reads —
        # a trap this fix's own written proposal walked into before it was implemented.
        if intent is AdapterIntent.READ and broker in _REAL_BROKERS:
            if real is None:
                raise StageRejected(_paper_read_unanswerable_message(broker))
            return real
        return sim_adapter  # paper intercept: placements never reach a venue
    if stage is Stage.MICRO_LIVE:
        if broker in _REAL_BROKERS:
            if real is None:
                raise StageRejected(_micro_live_unconfigured_message(broker))
            return real
        raise StageRejected(f"stage 'micro_live' has no installed adapter for broker '{broker}'")
    raise StageRejected("stage 'live' stays gated in Phase 4 — no real-money default")


def _real_adapter_for(
    broker: Broker,
    liberator_adapter: BrokerAdapter | None,
    streaming_pro_adapter: BrokerAdapter | None,
) -> BrokerAdapter | None:
    """The configured real adapter for this broker, or None (sim ignores broker)."""
    if broker is Broker.LIBERATOR:
        return liberator_adapter
    if broker is Broker.STREAMING_PRO:
        return streaming_pro_adapter
    return None


def _paper_read_unanswerable_message(broker: Broker) -> str:
    """Why a `paper` read refused — never silently substituted ([[TK-0488]])."""
    return (
        f"stage 'paper': no '{broker}' runtime is configured on this node, so a broker READ "
        f"cannot be answered — and will NOT be faked. An empty list or a default balance here "
        f"would be indistinguishable from a real one. Query GET /capabilities: "
        f"adapter_installed=false for this broker on this deployment."
    )


def _micro_live_unconfigured_message(broker: Broker) -> str:
    if broker is Broker.STREAMING_PRO:
        return (
            "stage 'micro_live' requires a configured streaming_pro runtime "
            "(owner mode + EXECUTION_ENGINE_STREAMING_PRO_API_KEY)"
        )
    return (
        "stage 'micro_live' requires a configured liberator runtime "
        "(owner mode + EXECUTION_ENGINE_LIBERATOR_API_KEY/PIN)"
    )

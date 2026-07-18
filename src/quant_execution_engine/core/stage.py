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
        if intent is AdapterIntent.READ and real is not None:
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

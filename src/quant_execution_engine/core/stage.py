"""The ``EXECUTION_ENGINE_STAGE`` safety ladder (E2) — the route gate.

Phase 3 matrix (decision log):

* ``sim``        — every broker routes to the deterministic ``SimAdapter``
  (sim is a STAGE, not a broker: the broker field is deliberately ignored).
* ``paper``      — TRADE intent is intercepted to sim (no real orders); READ
  intent for ``broker=liberator`` reaches the live Liberator session when one
  is configured (account/position realism).
* ``micro_live`` — ``broker=liberator`` routes to the real ``LiberatorAdapter``
  (PTRM caps enforce smallest size); every other broker is rejected.
* ``live``       — stays gated in Phase 3; always rejected.
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


def resolve_adapter(
    stage: Stage,
    broker: Broker,
    *,
    sim_adapter: BrokerAdapter,
    liberator_adapter: BrokerAdapter | None = None,
    intent: AdapterIntent = AdapterIntent.TRADE,
) -> BrokerAdapter:
    """Return the adapter the ladder permits, or raise :class:`StageRejected`."""
    if stage is Stage.SIM:
        return sim_adapter
    if stage is Stage.PAPER:
        if (
            intent is AdapterIntent.READ
            and broker is Broker.LIBERATOR
            and liberator_adapter is not None
        ):
            return liberator_adapter
        return sim_adapter  # paper intercept: placements never reach a venue
    if stage is Stage.MICRO_LIVE:
        if broker is Broker.LIBERATOR:
            if liberator_adapter is None:
                raise StageRejected(
                    "stage 'micro_live' requires a configured liberator runtime "
                    "(owner mode + EXECUTION_ENGINE_LIBERATOR_API_KEY/PIN)"
                )
            return liberator_adapter
        raise StageRejected(f"stage 'micro_live' has no installed adapter for broker '{broker}'")
    raise StageRejected("stage 'live' stays gated in Phase 3 — no real-money default")

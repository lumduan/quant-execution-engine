"""The ``EXECUTION_ENGINE_STAGE`` safety ladder (E2) — the route gate.

``sim`` and ``paper`` route every broker to the deterministic ``SimAdapter``
(paper's live broker *reads* land in Phase 3 with the first real adapter).
``micro_live``/``live`` have no installed adapter in Phase 2 and are rejected
with a typed error — no real-money path exists.
"""

from __future__ import annotations

from src.quant_execution_engine.adapters.base import BrokerAdapter
from src.quant_execution_engine.contracts.enums import Broker, Stage
from src.quant_execution_engine.contracts.errors import StageRejected

_SIM_STAGES = frozenset({Stage.SIM, Stage.PAPER})


def resolve_adapter(stage: Stage, broker: Broker, *, sim_adapter: BrokerAdapter) -> BrokerAdapter:
    """Return the adapter the ladder permits, or raise :class:`StageRejected`."""
    if stage in _SIM_STAGES:
        return sim_adapter
    raise StageRejected(
        f"stage '{stage}' has no installed adapter for broker '{broker}' "
        "(real adapters land in Phases 3-4; no real-money path exists in Phase 2)"
    )

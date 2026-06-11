"""SettradeAdapter package — re-implements the Settrade Open API v2 wire (Phase 4).

The official ``settrade-v2`` SDK is sync ``requests`` (forbidden in ``src/``) with
import-time side effects (config-file write, NTP call, version-check HTTP), so the
wire is re-implemented directly on ``httpx.AsyncClient`` (Design Decision 2). This
package currently ships the transport client, wire models, and errors; the adapter,
mapping, heartbeat, reconciler, and runtime land in later steps.
"""

from __future__ import annotations

from src.quant_execution_engine.adapters.settrade.client import (
    RateBudget,
    SettradeClient,
    redact_path,
    redact_payload,
    sign_content,
)
from src.quant_execution_engine.adapters.settrade.errors import (
    SettradeAdapterError,
    SettradeAuthError,
    SettradeMappingError,
    SettradeTransportError,
    SettradeVenueRejection,
)

__all__ = [
    "RateBudget",
    "SettradeAdapterError",
    "SettradeAuthError",
    "SettradeClient",
    "SettradeMappingError",
    "SettradeTransportError",
    "SettradeVenueRejection",
    "redact_path",
    "redact_payload",
    "sign_content",
]

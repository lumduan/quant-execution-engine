"""SettradeAdapter package — re-implements the Settrade Open API v2 wire (Phase 4).

The official ``settrade-v2`` SDK is sync ``requests`` (forbidden in ``src/``) with
import-time side effects (config-file write, NTP call, version-check HTTP), so the
wire is re-implemented directly on ``httpx.AsyncClient`` (Design Decision 2). This
package ships the transport client, wire models, errors, the ``SettradeAdapter``
(place/cancel/native amend/reads/heartbeat), and its process-singleton runtime +
heartbeat/reconcile workers.
"""

from __future__ import annotations

from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
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
from src.quant_execution_engine.adapters.settrade.runtime import (
    close_settrade_runtime,
    create_settrade_runtime,
    get_settrade_adapter,
    settrade_enabled,
    start_settrade_workers,
)

__all__ = [
    "RateBudget",
    "SettradeAdapter",
    "SettradeAdapterError",
    "SettradeAuthError",
    "SettradeClient",
    "SettradeMappingError",
    "SettradeTransportError",
    "SettradeVenueRejection",
    "close_settrade_runtime",
    "create_settrade_runtime",
    "get_settrade_adapter",
    "redact_path",
    "redact_payload",
    "settrade_enabled",
    "sign_content",
    "start_settrade_workers",
]

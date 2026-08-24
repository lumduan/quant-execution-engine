"""Streaming-Pro-adapter errors (module-local, shared base per convention)."""

from __future__ import annotations

from typing import ClassVar

from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.contracts.errors import OrderRejectedError


class StreamingProAdapterError(AdapterError):
    """Base for every Streaming-Pro-adapter failure."""


class StreamingProTransportError(StreamingProAdapterError):
    """Connectivity/timeout/5xx/non-JSON failure reaching settrade-streaming-api.

    These feed the session circuit breaker (§G); a structured bridge/venue
    rejection (a 2xx ``{ok:false}`` or a 4xx ``{detail}``) is NOT a transport
    error — it travels as a rejected ack carrying the reason.
    """


class StreamingProMappingError(StreamingProAdapterError):
    """The order cannot be expressed on the bridge wire (pre-flight).

    Raised before any HTTP I/O; the adapter converts it into a rejected ack so
    the reason persists durably — never a silent drop.
    """


class StreamingProAccountUnavailable(OrderRejectedError):
    """``account-info`` carried no balance for this account.

    ⚠️ Raised rather than degrading to ``Decimal("0")`` ([[TK-0396]]). SP's balance read is
    **SET-only** (the bridge hardcodes the ``fis`` segment), so a TFEX account produces exactly
    this — and a zero would have made an unreadable account look like a flat one.
    """

    code: ClassVar[str] = "streaming_pro_account_unavailable"

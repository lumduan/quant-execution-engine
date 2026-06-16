"""Streaming-Pro-adapter errors (module-local, shared base per convention)."""

from __future__ import annotations

from src.quant_execution_engine.adapters.errors import AdapterError


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

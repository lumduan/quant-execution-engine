"""Liberator-adapter errors (module-local, shared base per convention)."""

from __future__ import annotations

from src.quant_execution_engine.adapters.errors import AdapterError


class LiberatorAdapterError(AdapterError):
    """Base for every Liberator-adapter failure."""


class LiberatorTransportError(LiberatorAdapterError):
    """Connectivity/timeout/5xx/non-JSON failure reaching liberator-trading-api.

    These are the failures that feed the session circuit breaker (§G); a
    structured venue rejection is NOT a transport error — it travels as a
    rejected ack with the venue's reason.
    """


class LiberatorMappingError(LiberatorAdapterError):
    """The order cannot be expressed on the Liberator wire (pre-flight).

    Raised before any HTTP I/O; the adapter converts it into a rejected ack so
    the reason persists durably — never a silent drop.
    """

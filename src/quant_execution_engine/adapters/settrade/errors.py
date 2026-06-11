"""Settrade-adapter errors (module-local, shared base per convention).

Mirrors ``adapters/liberator/errors.py``: a structured venue rejection is NOT a
transport failure — it travels typed (and does NOT feed the circuit breaker),
whereas connectivity/timeout/5xx/non-JSON and auth failures are breaker food.
"""

from __future__ import annotations

from src.quant_execution_engine.adapters.errors import AdapterError


class SettradeAdapterError(AdapterError):
    """Base for every Settrade-adapter failure."""


class SettradeTransportError(SettradeAdapterError):
    """Connectivity/timeout/HTTP>=500/non-JSON failure reaching Settrade Open API.

    These are the failures that feed the session circuit breaker; a structured
    venue rejection (``{code, message}`` with status < 500) is NOT a transport
    error — it travels as :class:`SettradeVenueRejection`.
    """


class SettradeAuthError(SettradeTransportError):
    """OAuth login/refresh failure or an unrecoverable 401 (breaker food).

    A subclass of :class:`SettradeTransportError`: auth is part of session
    liveness, so a dead auth must trip the breaker like any other wire failure.
    Credentials never appear in the message — only the venue code/message fields.
    """


class SettradeMappingError(SettradeAdapterError):
    """The order cannot be expressed on the Settrade wire (pre-flight).

    Raised before any HTTP I/O; the adapter converts it into a rejected ack so
    the reason persists durably — never a silent drop.
    """


class SettradeMarketNotConfigured(SettradeAdapterError):
    """No OAuth broker app is configured for the requested market (Phase 4.1).

    Raised by the reconciler-facing ``fetch_venue_orders`` when a market has no
    client — NEVER an empty list (an empty list would forge "venue says zero
    orders" and drive cancel_confirm/ack_lost transitions against possibly-live
    orders). The reconciler treats it as a group-skip so the affected rows freeze
    and nag rather than transition on fabricated truth. Carries no venue code.
    """


class SettradeVenueRejection(SettradeAdapterError):
    """A structured non-2xx ``{code, message}`` venue rejection (status < 500).

    NOT breaker food — this is venue truth (bad symbol, price band, margin), not
    a wire failure. Carries the venue's ``code`` and the HTTP status so callers
    can map it onto the typed-rejection envelope without re-parsing the body.
    """

    def __init__(self, venue_code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.venue_code = venue_code
        self.status_code = status_code

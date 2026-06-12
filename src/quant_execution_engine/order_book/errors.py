"""Module-local exceptions for the order-book service (Phase 5).

All inherit the service-wide base
:class:`~src.quant_execution_engine.errors.ExecutionEngineError` (never a bare
``Exception``). ``OrderBookError`` is the subpackage base; the rest are raised by
providers and the router.
"""

from __future__ import annotations

from src.quant_execution_engine.errors import ExecutionEngineError


class OrderBookError(ExecutionEngineError):
    """Base for every order-book-subpackage error."""


class ProviderError(OrderBookError):
    """A provider failed to start, connect, or deliver (failover food)."""


class TicketAcquisitionError(ProviderError):
    """Could not obtain a Liberator ws-ticket (auth/transport failure)."""


class SymbolResolutionError(ProviderError):
    """Could not resolve a symbol to its venue order-book id."""


class ProviderNotConfigured(OrderBookError):
    """A market has no resolvable provider credentials (e.g. partial trio)."""

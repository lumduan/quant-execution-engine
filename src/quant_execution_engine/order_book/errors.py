"""Module-local exceptions for the order-book service (Phase 5).

All inherit the service-wide base
:class:`~src.quant_execution_engine.errors.ExecutionEngineError` (never a bare
``Exception``). ``OrderBookError`` is the subpackage base; the rest are raised by
providers and the router.
"""

from __future__ import annotations

from typing import ClassVar

from src.quant_execution_engine.contracts.errors import OrderRejectedError
from src.quant_execution_engine.errors import ExecutionEngineError


class OrderBookError(ExecutionEngineError):
    """Base for every order-book-subpackage error."""


class OrderBookUnavailable(OrderRejectedError):
    """No fresh book is cached for the symbol (or the service is disabled).

    Routes through the shared typed-error envelope exactly like
    ``order_not_found`` — the ``code`` maps to ``404`` in ``error_handlers``.
    These reads carry no order data, so the rejection is informational only.
    """

    code: ClassVar[str] = "order_book_unavailable"


class ProviderError(OrderBookError):
    """A provider failed to start, connect, or deliver (failover food)."""


class TicketAcquisitionError(ProviderError):
    """Could not obtain a Liberator ws-ticket (auth/transport failure)."""


class SymbolResolutionError(ProviderError):
    """Could not resolve a symbol to its venue order-book id."""


class ProviderNotConfigured(OrderBookError):
    """A market has no resolvable provider credentials (e.g. partial trio)."""

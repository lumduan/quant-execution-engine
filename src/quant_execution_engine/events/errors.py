"""Module-local exceptions for the order-update stream (Phase 5).

All inherit the service-wide base
:class:`~src.quant_execution_engine.errors.ExecutionEngineError` (never a bare
``Exception``). The wire-visible one routes through the shared typed-error
envelope exactly like the order rejections.
"""

from __future__ import annotations

from typing import ClassVar

from src.quant_execution_engine.contracts.errors import OrderRejectedError
from src.quant_execution_engine.errors import ExecutionEngineError


class EventStreamError(ExecutionEngineError):
    """Base for every order-update-stream-subpackage error."""


class OrderStreamUnavailable(OrderRejectedError):
    """The event hub is not running (lifespan not started).

    A server-side not-ready, not a missing resource — the ``code`` maps to
    ``503 SERVICE_UNAVAILABLE`` in ``error_handlers`` (cf. ``order_book_unavailable``,
    which is a 404 cold-cache miss). The stream carries no order data, so the
    rejection is informational only.
    """

    code: ClassVar[str] = "order_stream_unavailable"

"""Order-book service (Phase 5): normalized, read-only L2 cache.

A Liberator-WebSocket-fed in-engine cache that holds normalized
:class:`~src.quant_execution_engine.order_book.models.OrderBook` snapshots in
memory only — no durable storage, no canonical ownership (ADR D17). The whole
service defaults OFF (``EXECUTION_ENGINE_ORDER_BOOK_ENABLED``, D24).
"""

from __future__ import annotations

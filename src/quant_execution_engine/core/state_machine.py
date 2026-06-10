"""The frozen 9-state order machine (Phase 0 ADR §E) — pure, zero I/O.

Exactly the 13 edges the Phase-1 DB trigger (``execution.orders_guard``)
encodes; this module is the app-side guard that produces clean typed errors
before a DB round-trip, with the trigger as the backstop. Edges NOT in the
frozen graph (venue cancel-reject, fills while PENDING_CANCEL, Liberator's
PENDING_REPLACE -> CANCELLED end state) require an ADR amendment + an
infra-db migration before they may appear here.
"""

from __future__ import annotations

from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.contracts.errors import IllegalTransition

ENTRY_STATE = OrderState.PENDING_NEW

TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
)

LEGAL_EDGES: frozenset[tuple[OrderState, OrderState]] = frozenset(
    {
        (OrderState.PENDING_NEW, OrderState.NEW),
        (OrderState.PENDING_NEW, OrderState.REJECTED),
        (OrderState.NEW, OrderState.PARTIALLY_FILLED),
        (OrderState.NEW, OrderState.FILLED),
        (OrderState.NEW, OrderState.EXPIRED),
        (OrderState.NEW, OrderState.PENDING_CANCEL),
        (OrderState.NEW, OrderState.PENDING_REPLACE),
        (OrderState.PARTIALLY_FILLED, OrderState.FILLED),
        (OrderState.PARTIALLY_FILLED, OrderState.EXPIRED),
        (OrderState.PARTIALLY_FILLED, OrderState.PENDING_CANCEL),
        (OrderState.PARTIALLY_FILLED, OrderState.PENDING_REPLACE),
        (OrderState.PENDING_CANCEL, OrderState.CANCELLED),
        (OrderState.PENDING_REPLACE, OrderState.NEW),
    }
)


def is_terminal(state: OrderState) -> bool:
    """True for FILLED / CANCELLED / REJECTED / EXPIRED."""
    return state in TERMINAL_STATES


def is_legal(from_state: OrderState, to_state: OrderState) -> bool:
    """Same-state is a legal no-op; anything else must be a frozen edge."""
    if from_state is to_state:
        return True
    return (from_state, to_state) in LEGAL_EDGES


def assert_legal(
    from_state: OrderState,
    to_state: OrderState,
    *,
    client_order_id: str | None = None,
) -> None:
    """Raise :class:`IllegalTransition` for any move outside the frozen graph."""
    if not is_legal(from_state, to_state):
        raise IllegalTransition(
            f"illegal order status transition {from_state} -> {to_state}",
            client_order_id=client_order_id,
        )

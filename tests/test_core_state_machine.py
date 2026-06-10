"""The frozen 13-edge graph — exhaustively."""

from __future__ import annotations

import pytest
from src.quant_execution_engine.contracts.enums import OrderState
from src.quant_execution_engine.contracts.errors import IllegalTransition
from src.quant_execution_engine.core import state_machine


def test_exactly_thirteen_frozen_edges() -> None:
    assert len(state_machine.LEGAL_EDGES) == 13


def test_all_legal_edges_pass() -> None:
    for from_state, to_state in state_machine.LEGAL_EDGES:
        assert state_machine.is_legal(from_state, to_state)
        state_machine.assert_legal(from_state, to_state)


def test_same_state_is_a_legal_noop() -> None:
    for state in OrderState:
        assert state_machine.is_legal(state, state)


def test_exhaustive_illegal_complement_rejected() -> None:
    """Every (from, to) pair outside the frozen edges + identity raises."""
    for from_state in OrderState:
        for to_state in OrderState:
            if from_state is to_state:
                continue
            if (from_state, to_state) in state_machine.LEGAL_EDGES:
                continue
            assert not state_machine.is_legal(from_state, to_state)
            with pytest.raises(IllegalTransition):
                state_machine.assert_legal(from_state, to_state, client_order_id="x")


def test_terminal_states_have_no_outgoing_edges() -> None:
    for terminal in state_machine.TERMINAL_STATES:
        assert state_machine.is_terminal(terminal)
        outgoing = {e for e in state_machine.LEGAL_EDGES if e[0] is terminal}
        assert outgoing == set()


def test_entry_state_is_pending_new() -> None:
    assert state_machine.ENTRY_STATE is OrderState.PENDING_NEW
    assert not state_machine.is_terminal(OrderState.PENDING_NEW)

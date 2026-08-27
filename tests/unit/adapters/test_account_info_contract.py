"""The AccountInfo contract (TK-0396) — absence vs zero, and the margin block's discriminator."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError
from src.quant_execution_engine.adapters.base import AccountInfo, AccountType


def _cash(**kw: object) -> AccountInfo:
    return AccountInfo(account="70412572", buying_power=Decimal("1"), **kw)  # type: ignore[arg-type]


def test_absent_is_none_and_none_is_not_zero() -> None:
    """🔴 The contract-level statement of what TK-0396 was about.

    A broker that does not report a field must leave it None. If absence were modelled as
    Decimal("0"), a caller could not tell "this venue sends no margin data" from "margin is
    zero" — which is exactly the ambiguity that let get_account report 0 for funded accounts.
    """
    info = _cash()
    for field in ("cash_balance", "credit_limit", "withdrawable", "equity"):
        assert getattr(info, field) is None
        assert getattr(info, field) != Decimal("0")  # the whole point, stated explicitly


def test_margin_block_is_FORBIDDEN_on_a_non_derivative_account() -> None:
    """Not merely optional — forbidden. Enforced in BOTH directions, per NormalizedOrder's idiom.

    Modelling these as plain optionals would let a CASH account carry a margin figure that
    cannot exist on it, and nothing would object.
    """
    for field in ("equity", "excess_equity", "initial_margin", "maintenance_margin"):
        with pytest.raises(ValidationError, match="only valid on a DERIVATIVE account"):
            _cash(**{field: Decimal("5")})
        # ...and the same value is accepted once the discriminator says DERIVATIVE
        assert (
            AccountInfo(
                account="70173297",
                account_type=AccountType.DERIVATIVE,
                buying_power=Decimal("1"),
                **{field: Decimal("5")},
            )
            is not None
        )


def test_a_derivative_account_may_still_report_no_margin() -> None:
    """Deliberately NOT required-when: coverage is asymmetric, and that is not an error.

    A DERIVATIVE account read from a broker that reports no margin block is legitimately
    all-None. Requiring the fields would force such an adapter to invent them.
    """
    info = AccountInfo(
        account="70173297", account_type=AccountType.DERIVATIVE, buying_power=Decimal("1")
    )
    assert info.equity is None


def test_money_serialises_as_a_STRING_on_the_wire() -> None:
    """Decimal-at-the-boundary. Before this change AccountInfo used bare Decimal, so every
    money field would have gone out as a JSON *number* the moment /account shipped."""
    info = AccountInfo(
        account="70173297",
        account_type=AccountType.DERIVATIVE,
        buying_power=Decimal("13506.72"),
        equity=Decimal("13506.72"),
    )
    wire = json.loads(info.model_dump_json())
    assert wire["buying_power"] == "13506.72", "money must not be a JSON number"
    assert isinstance(wire["equity"], str)


def test_buying_power_is_still_required_so_existing_callers_are_unaffected() -> None:
    with pytest.raises(ValidationError):
        AccountInfo(account="X")  # type: ignore[call-arg]


def test_two_legs_on_ONE_symbol_stay_DISTINCT_rows() -> None:
    """🔑 The composite-key property, asserted so it cannot regress silently.

    `session:lib-research` (TK-0444/#237): a venue position row is keyed by
    ``(symbolDisplay, sideShow)`` — the venue's own client merges on the pair, so one
    symbol can legitimately be two rows. Before ``side`` existed, `Position` was keyed on
    symbol alone and these two legs were **indistinguishable**: any set/dict keyed by
    symbol collapsed them, and a netting consumer destroyed the information outright.

    This is decidable without ever observing a populated venue response, which is why it
    is fixed now rather than waiting for one.
    """
    from src.quant_execution_engine.adapters.base import Position
    from src.quant_execution_engine.contracts.enums import Market, Side

    # ⚠️ Everything EXCEPT side is identical, deliberately. An earlier version gave the
    # legs different net_qty — so they stayed distinct even with `side` removed, and the
    # test passed for a reason unrelated to what it claims. A mutation removing the field
    # SURVIVED it. Side must be the only difference or this asserts nothing.
    long_leg = Position(account="A", market=Market.TFEX, symbol="S50Z26", net_qty=3, side=Side.BUY)
    short_leg = Position(
        account="A", market=Market.TFEX, symbol="S50Z26", net_qty=3, side=Side.SELL
    )

    assert long_leg != short_leg
    assert len({long_leg, short_leg}) == 2, "two legs on one symbol must not collapse"
    # And the pre-fix failure, spelled out: symbol alone is NOT a key.
    assert long_leg.symbol == short_leg.symbol


def test_side_None_means_THE_VENUE_DID_NOT_SAY_not_flat_and_not_long() -> None:
    """``None`` is an absence of information, not a value — the TK-0396 discipline again.

    A SET equity row legitimately carries ``None`` (cannot be short). A derivatives row
    carrying ``None`` is an adapter gap. Either way it must never be read as a side.
    """
    from src.quant_execution_engine.adapters.base import Position
    from src.quant_execution_engine.contracts.enums import Market, Side

    set_row = Position(account="A", market=Market.SET, symbol="PTT", net_qty=100)
    assert set_row.side is None
    assert set_row.side is not Side.BUY

"""EH6 — the real-routing authority guard (core/routing_authority.py)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import SecretStr
from src.quant_execution_engine.adapters.sim import SimAdapter
from src.quant_execution_engine.config.errors import ConfigError
from src.quant_execution_engine.contracts.enums import Broker, Stage
from src.quant_execution_engine.core.routing_authority import (
    RealRoutingNotAuthorized,
    assert_may_route_real,
    assert_startup_declaration,
)

from tests._fakes import StubBrokerAdapter
from tests.conftest import make_settings


def _sim() -> SimAdapter:
    return SimAdapter(default_fill_price=Decimal("1"))


def _call(*, adapter: object, account: str = "70173292", **overrides: object) -> None:
    sim = _sim()
    assert_may_route_real(
        settings=make_settings(**overrides),
        account=account,
        broker=Broker.LIBERATOR,
        adapter=adapter,  # type: ignore[arg-type]
        sim_adapter=sim if adapter is not None else sim,
    )


# --- the invariant --------------------------------------------------------------


def test_a_real_adapter_with_NO_declaration_is_refused() -> None:
    """🔴 The whole point: absent ⇒ refuse. Never absent ⇒ allow.

    A node that was never told which accounts it is authoritative for may route nothing real.
    Today the invariant holds because HOME happens to route only sim (285/285 measured) — this
    test is what makes it hold when that circumstance changes.
    """
    with pytest.raises(RealRoutingNotAuthorized, match="not declared the real router for any"):
        _call(adapter=StubBrokerAdapter())


def test_a_real_adapter_for_an_UNDECLARED_account_is_refused() -> None:
    """A declaration is per-account, not a global on-switch."""
    with pytest.raises(RealRoutingNotAuthorized, match="not in this node's real-routing"):
        _call(adapter=StubBrokerAdapter(), account="70412572", real_routing_accounts=["70173292"])


def test_a_declared_account_is_permitted() -> None:
    """The positive control. Without it, 'it refuses' is met by a guard that refuses everything."""
    _call(adapter=StubBrokerAdapter(), account="70173292", real_routing_accounts=["70173292"])


def test_an_empty_string_declaration_does_not_authorize_anything() -> None:
    """`EXECUTION_ENGINE_REAL_ROUTING_ACCOUNTS=""` must not read as "declared"."""
    with pytest.raises(RealRoutingNotAuthorized):
        _call(adapter=StubBrokerAdapter(), real_routing_accounts=["", "   "])


def test_sim_routing_is_never_touched() -> None:
    """The guard binds on the RESOLVED ADAPTER being real, not on the stage.

    At paper a placement is intercepted to SimAdapter and reaches no venue, so gating on stage
    would block harmless traffic while still missing any future stage. Binding on the object
    that would actually be contacted is both narrower and more durable.
    """
    sim = _sim()
    for stage in Stage:
        # micro_live/live cannot even be constructed without a declaration (see the boot
        # tests), so give them a valid one -- the point here is that sim passes regardless.
        extra = (
            {"real_routing_accounts": ["70173292"], "liberator_api_key": SecretStr("k")}
            if stage in (Stage.MICRO_LIVE, Stage.LIVE)
            else {}
        )
        assert_may_route_real(
            settings=make_settings(stage=stage, **extra),
            account="an-account-that-is-NOT-declared",
            broker=Broker.LIBERATOR,
            adapter=sim,
            sim_adapter=sim,
        )


# --- boot-time refusal ----------------------------------------------------------


def test_startup_REFUSES_micro_live_without_a_declaration() -> None:
    """Refuse at start-up, not per-order.

    AWS_STANDUP records why: *"a defect that can only fire at micro_live is one that soaking
    at sim cannot reach."* This test constructs micro_live directly for exactly that reason —
    no soak can substitute.
    """
    with pytest.raises(ConfigError, match="REAL_ROUTING_ACCOUNTS"):
        assert_startup_declaration(
            make_settings(stage=Stage.MICRO_LIVE, liberator_api_key=SecretStr("k"))
        )


def test_startup_REFUSES_a_declaration_that_disagrees_with_the_credentials_held() -> None:
    """🔑 The cross-check, and the reason it exists.

    EH6's invariant once rested on EH7's account map; that map was wrong for 31 days and a
    guard keyed to it would have stood the WRONG node down. A node cannot be mistaken about
    which credentials it holds. So a declaration with no broker credential behind it is a
    contradiction, and it is REFUSED rather than resolved — picking a winner is how the 31
    days happened.
    """
    with pytest.raises(ConfigError, match="disagree"):
        assert_startup_declaration(
            make_settings(stage=Stage.MICRO_LIVE, real_routing_accounts=["70173292"])
        )


def test_startup_permits_a_declaration_backed_by_a_credential() -> None:
    """Positive control — without it, 'it refuses' is met by a check that refuses everything."""
    assert_startup_declaration(
        make_settings(
            stage=Stage.MICRO_LIVE,
            real_routing_accounts=["70173292"],
            liberator_api_key=SecretStr("k"),
        )
    )


def test_startup_is_a_no_op_below_micro_live() -> None:
    """sim/paper reach no venue, so a declaration is not required to start there."""
    for stage in (Stage.SIM, Stage.PAPER):
        assert_startup_declaration(make_settings(stage=stage))


# --- wire contract --------------------------------------------------------------


def test_the_code_is_mapped_to_409_not_the_silent_400_fallback() -> None:
    """An unmapped code falls back to 400, so 'not 500' would pass for a forgotten mapping.
    Pin presence AND the exact status — and that it stays DISTINCT from stage_rejected, since
    'the ladder said no' and 'this node is not that account's router' are different failures."""
    from src.quant_execution_engine.api.error_handlers import _STATUS_BY_CODE, _status_for
    from src.quant_execution_engine.contracts.errors import StageRejected

    exc = RealRoutingNotAuthorized("x")
    assert exc.code in _STATUS_BY_CODE, "missing -> silent 400 fallback"
    assert _status_for(exc) == 409
    assert _status_for(exc) != _status_for(StageRejected("x"))

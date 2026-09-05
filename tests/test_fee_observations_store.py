"""🔒 The ``execution.fee_observations`` store — write path, read path, and time domain.

The store's whole job is to make a *later* comparison trustworthy, so the failures that
matter here are the ones that produce a **plausible wrong number** rather than an error:

* an observation filed under the wrong venue month (tiers accrue monthly — a boundary
  error silently compares September against August's tier);
* a row stored with no verdict, or with a verdict computed against a different day's basis;
* an ``indicative_quote`` averaged with a ``charged`` amount;
* a ``JSONB`` column arriving as a string that *looks* like data.

None of those raise. Every test below exists because the correct and incorrect versions are
indistinguishable after the fact.
"""

from __future__ import annotations

import inspect
import json
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from src.quant_execution_engine.db import fee_observations as store
from src.quant_execution_engine.db.errors import RepositoryError
from src.quant_execution_engine.db.fee_observations import (
    FeeObservation,
    FeeObservationRow,
    ObservationKind,
    fetch_latest,
    fetch_observations,
    insert_observation,
    month_to_date_contracts,
    next_fee_month,
    record_observation,
    venue_date,
    venue_fee_month,
)
from src.quant_execution_engine.reference.fee_schedule import (
    FeeSchedule,
    Verdict,
    append_entry,
    load_fee_schedule,
    render_entry,
)

from tests._fakes import FakeConn, FakePool

_CANON = Path("src/quant_execution_engine/reference/fee_schedule.toml")
_MODULE = Path("src/quant_execution_engine/db/fee_observations.py")

# 2026-08-31 20:00 UTC is 2026-09-01 03:00 in Bangkok — a DIFFERENT month.
_BOUNDARY = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


@pytest.fixture
def schedule() -> FeeSchedule:
    """A copy of the canonical TOML — a test must never read the real one by accident."""
    return load_fee_schedule(_CANON)


def _obs(**over: Any) -> FeeObservation:
    """A baseline observation; ``**over`` replaces individual fields.

    Built with ``dataclasses.replace`` rather than a dict splat so mypy still type-checks
    every override — a ``dict[str, object]`` splat would type-check as nothing at all, and a
    test helper that silently accepts a wrong type is how a wrong fixture survives.
    """
    base = FeeObservation(
        observed_at=datetime(2026, 9, 5, 3, 30, tzinfo=UTC),
        kind=ObservationKind.INDICATIVE_QUOTE,
        source="unit-test",
        broker="liberator",
        account="70000007",  # allowlisted synthetic: liberator TFEX/derivative
        symbol="S50Z26",
        contract_month="Z26",
        side="Long",
        quantity=1,
        price=Decimal("700.0"),
        raw_response={"probe": True},
    )
    return replace(base, **over) if over else base


# One asyncpg-shaped record. `raw_response` is a STRING here because that is what asyncpg
# actually sends for JSONB without a codec — a fixture holding a dict would test the decoder
# against input it never receives.
_RECORD: dict[str, Any] = {
    "id": 1,
    "observed_at": datetime(2026, 9, 5, tzinfo=UTC),
    "observation_kind": "indicative_quote",
    "source": "s",
    "broker": "liberator",
    "account": "70000007",
    "symbol": "S50Z26",
    "contract_month": "Z26",
    "is_roll_boundary": False,
    "side": "Long",
    "quantity": 1,
    "price": Decimal("700"),
    "commission": None,
    "exchange_fee": None,
    "clearing_fee": None,
    "vat": None,
    "total_fee": None,
    "fee_month": date(2026, 9, 1),
    "month_to_date_contracts": None,
    "raw_response": '{"Commission Fee + VAT": "1.87"}',  # a STRING, as asyncpg sends it
    "basis_value": Decimal("14"),
    "basis_effective_from": date(2026, 9, 4),
    "verdict": "at_or_below",
    "inserted_at": datetime(2026, 9, 5, tzinfo=UTC),
}


# ────────────────────────────── the time domain ──────────────────────────────


def test_venue_date_is_BANGKOK_not_UTC_at_the_boundary() -> None:
    """🔑 The failure this module was most likely to have. It cannot raise.

    A tier accrues over the VENUE's month. Anchoring in UTC files an observation made in the
    first seven hours of a Bangkok month under the PREVIOUS month, against the wrong tier.
    """
    assert venue_date(_BOUNDARY) == date(2026, 9, 1), "20:00Z on Aug 31 is Sep 1 in Bangkok"
    assert venue_fee_month(_BOUNDARY) == date(2026, 9, 1), "September, not August"


def test_a_NAIVE_timestamp_is_read_as_UTC_not_as_local_time() -> None:
    """The platform stores UTC. ``astimezone`` on a naive value assumes LOCAL time instead.

    On a machine running in Bangkok that mistake is invisible; on a UTC host it shifts every
    boundary by seven hours. Pinned so the behaviour cannot depend on where the code runs.
    """
    assert venue_fee_month(datetime(2026, 8, 31, 20, 0)) == date(2026, 9, 1)


def test_next_fee_month_crosses_the_YEAR_boundary() -> None:
    """December must roll to January, not to month 13 — the classic off-by-one home."""
    assert next_fee_month(date(2026, 12, 1)) == date(2027, 1, 1)
    assert next_fee_month(date(2026, 2, 1)) == date(2026, 3, 1), "short month"
    assert next_fee_month(date(2028, 2, 1)) == date(2028, 3, 1), "leap February"


def test_fee_month_is_DERIVED_and_cannot_disagree_with_observed_at() -> None:
    """Two inputs that must agree will eventually disagree; one input cannot."""
    assert _obs(observed_at=_BOUNDARY).fee_month == date(2026, 9, 1)
    assert "fee_month" not in inspect.signature(FeeObservation).parameters, (
        "fee_month must not be settable — it is derived from observed_at"
    )


def test_the_MTD_query_uses_the_SINGLE_at_time_zone_form() -> None:
    """🔴 Regression guard for a bug that was in this file and raised nothing.

    ``exec_ts`` is TIMESTAMPTZ. The chained form ``AT TIME ZONE 'UTC' AT TIME ZONE
    'Asia/Bangkok'`` first yields a NAIVE UTC timestamp, then reads that naive value AS
    Bangkok time — landing 7 hours early, i.e. off by one day exactly at the month boundary,
    which is the only place a monthly-tier query can be wrong. Verified against Postgres:
    the chained form returned 2026-08-31 for an instant that is 2026-09-01 in Bangkok.

    Asserted on the SQL constant rather than the file text, so the comment that *documents*
    the wrong form does not satisfy the check.
    """
    sql = store._SUM_FILLED_QTY
    assert "AT TIME ZONE 'Asia/Bangkok'" in sql
    assert "AT TIME ZONE 'UTC'" not in sql, "the chained form is off by one day at the boundary"


# ────────────────────────────── the write path ──────────────────────────────


@pytest.mark.asyncio
async def test_record_observation_STORES_THE_VERDICT_it_computed(schedule: FeeSchedule) -> None:
    """A row without its verdict is a number nobody can act on."""
    conn = FakeConn(fetchval_results=[7])
    pool = FakePool(conn)
    result = await record_observation(
        pool,
        schedule,
        _obs(),
        instrument="s50_futures",
        field="commission",
        observed_value=Decimal("8"),
    )
    assert result.observation_id == 7
    _, sql, args = conn.calls[0]
    assert "INSERT INTO execution.fee_observations" in sql
    assert args[-3:] == (Decimal("14"), date(2026, 9, 4), "at_or_below"), (
        "basis_value, basis_effective_from and verdict must be the LAST three columns"
    )


@pytest.mark.asyncio
async def test_the_alert_is_ONE_SIDED(schedule: FeeSchedule) -> None:
    """Above alerts; at-or-below is silent. Direction, not magnitude.

    A basis that is too expensive makes a strategy look worse than it is — safe. Too cheap
    makes it look better, and in this umbrella that has already turned a losing result into
    an apparently positive one.
    """
    for observed, expect_alert, verdict in (
        (Decimal("8"), False, "at_or_below"),
        (Decimal("14"), False, "at_or_below"),  # equal is NOT above
        (Decimal("16.50"), True, "above"),
    ):
        conn = FakeConn(fetchval_results=[1])
        res = await record_observation(
            FakePool(conn),
            schedule,
            _obs(),
            instrument="s50_futures",
            field="commission",
            observed_value=observed,
        )
        assert res.should_alert is expect_alert, observed
        assert res.comparison.verdict.value == verdict, observed
        assert conn.calls[0][2][-1] == verdict, "the stored verdict must match the computed one"


@pytest.mark.asyncio
async def test_the_basis_is_resolved_as_of_THE_OBSERVATIONS_day_not_the_newest(
    tmp_path: Path,
) -> None:
    """🔑 Not "today", and not "the latest entry". Two entries make the difference visible.

    With a single-entry schedule this test would pass no matter which date the resolver
    used, so a second entry is appended first: a resolver that always took the newest would
    stamp 16.50 onto an observation from BEFORE that entry existed, and the stored verdict
    would then disagree with the row it sits on — silently, and only for rows old enough
    that nobody re-reads them.
    """
    scratch = tmp_path / "fee_schedule.toml"
    shutil.copy(_CANON, scratch)
    append_entry(
        scratch,
        render_entry(
            "s50_futures",
            "commission",
            value=Decimal("16.50"),
            unit="THB per contract per side",
            effective_from=date(2026, 9, 10),
            observed_on=date(2026, 9, 10),
            prior=Decimal("14"),
        ),
    )
    sched = load_fee_schedule(scratch)
    assert len(sched.series_for("s50_futures", "commission")) == 2, "the mutation must be applied"

    conn = FakeConn(fetchval_results=[1])
    await record_observation(
        FakePool(conn),
        sched,
        _obs(observed_at=datetime(2026, 9, 5, 3, 30, tzinfo=UTC)),  # BEFORE the 09-10 entry
        instrument="s50_futures",
        field="commission",
        observed_value=Decimal("15"),
    )
    basis, effective_from, verdict = conn.calls[0][2][-3:]
    assert basis == Decimal("14"), "must use the basis in force on 2026-09-05, not the newest"
    assert effective_from == date(2026, 9, 4)
    assert verdict == "above", "15 > 14 — and it would read at_or_below against 16.50"


@pytest.mark.asyncio
async def test_an_observation_PREDATING_every_entry_RAISES_rather_than_back_dating(
    schedule: FeeSchedule,
) -> None:
    """An observation from before any basis existed cannot be checked, and says so.

    Back-dating the earliest entry onto it would manufacture a verdict for a period the
    schedule makes no claim about — the row would look checked and would not be. Raising is
    the correct refusal; the caller may still bank it unchecked via ``insert_observation``
    with ``comparison=None``, which is honest because the NULLs are visible.
    """
    conn = FakeConn(fetchval_results=[1])
    with pytest.raises(KeyError, match="no entry effective on or before"):
        await record_observation(
            FakePool(conn),
            schedule,
            _obs(observed_at=_BOUNDARY),  # 2026-09-01 venue; earliest entry is 2026-09-04
            instrument="s50_futures",
            field="commission",
            observed_value=Decimal("8"),
        )
    assert conn.calls == [], "nothing may be written when the check could not be made"


@pytest.mark.asyncio
async def test_raw_response_is_serialised_as_JSON_for_the_jsonb_column() -> None:
    """asyncpg will not adapt a dict to ``jsonb``; it must be dumped, and cast in the SQL."""
    conn = FakeConn(fetchval_results=[1])
    await insert_observation(FakePool(conn), _obs(raw_response={"a": 1, "b": [2, 3]}))
    _, sql, args = conn.calls[0]
    assert "$19::jsonb" in sql, "the placeholder must be cast or Postgres sees text"
    assert json.loads(args[18]) == {"a": 1, "b": [2, 3]}


@pytest.mark.asyncio
async def test_a_GAP_with_no_basis_stores_NULLs_rather_than_a_fabricated_verdict() -> None:
    """Where the schedule records a GAP there is nothing to compare against.

    NULL is the honest value. Inventing a basis so the column is non-empty would make an
    unchecked row indistinguishable from a checked one.
    """
    conn = FakeConn(fetchval_results=[1])
    await insert_observation(FakePool(conn), _obs(), comparison=None)
    assert conn.calls[0][2][-3:] == (None, None, None)


@pytest.mark.asyncio
async def test_a_column_CHECK_violation_becomes_a_RepositoryError() -> None:
    """A bare 5xx reads as RETRYABLE; a schema mismatch is permanent and would retry forever.

    Same reasoning as TK-0395 in ``repositories.insert_order``.
    """
    conn = FakeConn(raise_map={"fee_observations": asyncpg.exceptions.CheckViolationError("bad")})
    with pytest.raises(RepositoryError, match="column CHECK"):
        await insert_observation(FakePool(conn), _obs())


@pytest.mark.asyncio
async def test_a_missing_GRANT_is_reported_as_a_GRANT_problem_not_a_missing_table() -> None:
    """🔴 The error message is the whole point of this test.

    ``information_schema`` filters by privilege, so a role with no rights on the table sees
    NOTHING rather than a permission error — the symptom reads as "the migration did not
    run" and the investigation starts in the wrong place. The message must send the reader
    to the GRANT. (quant-infra-db PR #31 fixed the real instance of this.)
    """
    conn = FakeConn(
        raise_map={"fee_observations": asyncpg.exceptions.InsufficientPrivilegeError("denied")}
    )
    with pytest.raises(RepositoryError, match="GRANT, not the migration"):
        await insert_observation(FakePool(conn), _obs())


# ────────────────────────────── the read path ──────────────────────────────


def test_every_read_REQUIRES_a_kind_and_defaults_to_nothing() -> None:
    """🔑 An indicative quote and a charged amount are different claims.

    A default would let a quote silently stand in for evidence of a charge. Asserted on the
    signature so no future edit can add a convenience default.
    """
    for fn in (fetch_observations, fetch_latest):
        param = inspect.signature(fn).parameters["kind"]
        assert param.default is inspect.Parameter.empty, f"{fn.__name__} must require kind"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{fn.__name__}: keyword-only"


def test_jsonb_arriving_as_a_STRING_is_decoded() -> None:
    """asyncpg returns ``jsonb`` as ``str`` unless a codec is registered.

    Undecoded, ``raw_response`` is a string that *looks* like data and fails only at the
    first key access — far from here, in whatever reads the evidence.
    """
    record = dict(_RECORD)
    row = FeeObservationRow.from_record(record)
    assert row.raw_response == {"Commission Fee + VAT": "1.87"}, "must be a dict, not a str"
    assert row.verdict is Verdict.AT_OR_BELOW
    assert isinstance(row.price, Decimal), "money is Decimal, never float"


@pytest.mark.asyncio
async def test_fetch_latest_returns_None_when_nothing_was_ever_observed() -> None:
    """``None`` means NOTHING IS KNOWN — it is not a zero-cost reading."""
    assert (
        await fetch_latest(FakePool(FakeConn()), kind=ObservationKind.CHARGED, symbol="X") is None
    )


# ────────────────────────────── structural ──────────────────────────────


def test_the_module_contains_NO_update_or_delete_path() -> None:
    """Append-only enforced in code, not only by the GRANT.

    The database refuses UPDATE/DELETE for ``quant`` today, but a grant is config and can be
    widened by someone solving an unrelated problem. Evidence that can be edited is not
    evidence, so the module offers no way to try.
    """
    text = _MODULE.read_text(encoding="utf-8")
    sql_lines = [ln for ln in text.splitlines() if '"' in ln and not ln.lstrip().startswith("#")]
    body = " ".join(sql_lines).upper()
    assert "UPDATE EXECUTION." not in body
    assert "DELETE FROM" not in body
    assert not [n for n in dir(store) if n.startswith(("update_", "delete_", "upsert_"))]


def test_the_store_is_NOT_REACHABLE_from_the_order_path() -> None:
    """A cost corroborator that can fail on a network call has no business in the order path.

    This is why the store is its own module rather than a function in ``repositories.py`` —
    that module IS imported by the router and both adapters, so anything added to it lands in
    the order path by construction.
    """
    import ast

    root = Path("src/quant_execution_engine")
    for mod in (
        root / "core" / "router.py",
        root / "adapters" / "liberator" / "adapter.py",
        root / "adapters" / "streaming_pro" / "adapter.py",
        root / "db" / "repositories.py",
    ):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert "fee_observation" not in n, f"{mod.name} imports {n} — now in the order path"


@pytest.mark.asyncio
async def test_month_to_date_bounds_are_HALF_OPEN_on_the_fee_month() -> None:
    """``[fee_month, next_fee_month)`` — inclusive start, EXCLUSIVE end.

    A closed upper bound would count the first day of the next month twice: once here and
    once in that month's own window. Tier volume would then be overstated at every boundary,
    in the direction that makes the account look like it has reached a better tier.
    """
    conn = FakeConn(fetchval_results=[42])
    total = await month_to_date_contracts(
        FakePool(conn),
        broker="liberator",
        account="70000007",
        market="TFEX",
        fee_month=date(2026, 12, 1),
    )
    assert total == 42
    _, sql, args = conn.calls[0]
    assert args[3] == date(2026, 12, 1), "inclusive lower bound"
    assert args[4] == date(2027, 1, 1), "exclusive upper bound, and it must cross the YEAR"
    assert ">= $4" in sql and "< $5" in sql, "half-open, not BETWEEN"


@pytest.mark.asyncio
async def test_fetch_latest_returns_the_row_when_one_exists() -> None:
    """The populated path — the ``None`` test alone would pass against a stub that never reads."""
    conn = FakeConn(fetchrow_results=[_RECORD])
    row = await fetch_latest(FakePool(conn), kind=ObservationKind.INDICATIVE_QUOTE, symbol="S50Z26")
    assert row is not None and row.id == 1
    assert conn.calls[0][2] == ("indicative_quote", "S50Z26"), "kind must be bound to $1"


def test_an_already_decoded_jsonb_dict_is_passed_through_unchanged() -> None:
    """If a codec IS registered, ``raw_response`` arrives as a dict and must not be re-parsed.

    ``json.loads`` on a dict raises, so the isinstance branch is load-bearing rather than
    defensive noise.
    """
    record = dict(_RECORD)
    record["raw_response"] = {"already": "decoded"}
    assert FeeObservationRow.from_record(record).raw_response == {"already": "decoded"}


@pytest.mark.asyncio
async def test_fetch_observations_binds_kind_month_and_symbol_and_orders_oldest_first() -> None:
    """The month read, with its three bound parameters asserted.

    A series is only comparable if every row in it came from the same month, symbol and
    KIND. Binding the wrong parameter to the wrong placeholder would return a plausible list
    of the wrong rows — which is indistinguishable from a correct one at the call site.
    """
    conn = FakeConn(fetch_results=[[dict(_RECORD), dict(_RECORD, id=2)]])
    rows = await fetch_observations(
        FakePool(conn),
        kind=ObservationKind.INDICATIVE_QUOTE,
        fee_month=date(2026, 9, 1),
        symbol="S50Z26",
    )
    assert [r.id for r in rows] == [1, 2]
    assert all(isinstance(r.raw_response, dict) for r in rows), "every row decoded, not just [0]"
    _, sql, args = conn.calls[0]
    assert args == ("indicative_quote", date(2026, 9, 1), "S50Z26")
    assert "ORDER BY observed_at" in sql, "oldest first — a series read backwards is not a series"

"""🔒 The charged-fee blotter sweep — the failures here all LOOK like success.

The data is **perishable at one venue day and cannot be backfilled**, so the expensive
failure is not a crash: it is a run that reports "swept, nothing to bank" when there was
something. Every test below separates a real zero from a blind one.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from src.quant_execution_engine.adapters.liberator.errors import LiberatorTransportError
from src.quant_execution_engine.observations import blotter_sweep
from src.quant_execution_engine.observations.blotter_sweep import (
    Money,
    SweepResult,
    _money,
    sweep_account,
    sweep_accounts,
)

from tests._fakes import FakeConn, FakePool

_ACCOUNT = "70000007"  # allowlisted synthetic: liberator TFEX/derivative
_NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def _envelope(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"raw_response": {"result": {"list": list(rows)}}}


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "orderNo": "71937953",
        "symbol": "S50Z26",
        "side": "B",
        "price": "900.5",
        "volume": 2,
        "matched": 2,
        "balance": 0,
        "cancelled": 0,
        "amount": "1801.0",
        "fee": "28.00",
        "vat": "1.96",
        "status": "M",
    }
    base.update(over)
    return base


class _Reader:
    """Returns scripted envelopes; records the paths requested."""

    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads)
        self.paths: list[str] = []

    async def get_json(self, path: str) -> dict[str, Any]:
        self.paths.append(path)
        p = self.payloads.pop(0) if self.payloads else _envelope()
        if isinstance(p, Exception):
            raise p
        return p


# ─────────────────────────── what gets banked, and what does not ───────────────────────────


@pytest.mark.asyncio
async def test_only_FILLED_rows_are_banked() -> None:
    """An unfilled order has not been charged. Banking its ``fee: 0`` would create rows that
    look like evidence of a free trade — and there are far more resting orders than fills."""
    reader = _Reader(_envelope(_row(matched=0, fee="0"), _row(orderNo="X2", matched=1)))
    conn = FakeConn(fetch_results=[[]], fetchval_results=[1])
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)
    assert res.rows_seen == 2
    assert res.filled_rows == 1, "matched=0 must not count as filled"
    assert res.banked == 1


@pytest.mark.asyncio
async def test_a_PARTIAL_fill_is_banked_because_it_HAS_been_charged() -> None:
    """`balance > 0` does not mean unfilled — the matched part was charged."""
    reader = _Reader(_envelope(_row(matched=1, balance=1)))
    conn = FakeConn(fetch_results=[[]], fetchval_results=[1])
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)
    assert res.banked == 1
    assert conn.calls[-1][2][9] == 1, "quantity must be the MATCHED qty, not the order volume"


@pytest.mark.asyncio
async def test_a_row_already_banked_is_not_banked_twice() -> None:
    """The sweep may run more than once a day; a duplicate charge row is a fabricated cost."""
    reader = _Reader(_envelope(_row(orderNo="71937953")))
    conn = FakeConn(fetch_results=[[{"order_no": "71937953"}]])
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)
    assert res.already_banked == 1 and res.banked == 0
    assert not any(c[0] == "fetchval" for c in conn.calls), "no INSERT may be attempted"


# ─────────────────────────── absent vs zero: the whole point ───────────────────────────


@pytest.mark.asyncio
async def test_an_ABSENT_fee_is_NULL_and_a_real_ZERO_is_ZERO() -> None:
    """🔑 ``None`` = the venue did not report it. ``0`` = the venue said zero.

    Collapsing them makes "we are blind" indistinguishable from "it was free". Both genuinely
    occur in the same payload, so this is not a theoretical distinction.
    """
    absent = _row(orderNo="C")
    absent.pop("fee")  # the venue simply did not send the key
    reader = _Reader(_envelope(_row(orderNo="A"), _row(orderNo="B", fee="0"), absent))
    conn = FakeConn(fetch_results=[[]], fetchval_results=[1, 2, 3])
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)

    commissions = [c[2][11] for c in conn.calls if c[0] == "fetchval"]
    assert commissions == [Decimal("28.00"), Decimal("0"), None]
    assert res.rows_with_a_fee_value == 2, "the absent one must NOT count as a reading"
    assert res.rows_with_a_NONZERO_fee == 1, "a real zero is a reading but not a NON-ZERO one"


@pytest.mark.asyncio
async def test_a_run_that_banks_only_ZERO_fees_has_NOT_answered_the_semantics() -> None:
    """🔴 TK-0524 §2's three unknowns need a NON-ZERO fee. Zeroes settle nothing.

    Without this flag a sweep could bank a hundred `fee: 0` rows and be reported as having
    produced charged-cost evidence, when the platform would still have none.
    """
    reader = _Reader(_envelope(_row(orderNo="A", fee="0"), _row(orderNo="B", fee="0")))
    conn = FakeConn(fetch_results=[[]], fetchval_results=[1, 2])
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)
    assert res.banked == 2
    assert res.semantics_are_now_answerable is False
    reader2 = _Reader(_envelope(_row(orderNo="C", fee="28.00")))
    conn2 = FakeConn(fetch_results=[[]], fetchval_results=[3])
    res2 = await sweep_account(reader2, FakePool(conn2), account=_ACCOUNT, now=_NOW)
    assert res2.semantics_are_now_answerable is True


# ─────────────────────────── it must not COMPARE ───────────────────────────


@pytest.mark.asyncio
async def test_nothing_is_compared_against_the_basis() -> None:
    """🔴 Deliberate. Whether ``fee`` is per-order, per-fill or cumulative is UNVERIFIED, as
    is whether it includes the regulator fee. Dividing by ``matched`` to reach a per-contract
    figure would store a confident wrong verdict that later reads exactly like a right one.
    """
    reader = _Reader(_envelope(_row()))
    conn = FakeConn(fetch_results=[[]], fetchval_results=[1])
    await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)
    basis, effective_from, verdict = conn.calls[-1][2][-3:]
    assert (basis, effective_from, verdict) == (None, None, None)
    src = Path("src/quant_execution_engine/observations/blotter_sweep.py").read_text("utf-8")
    assert "compare(" not in src, "the sweep must not call the one-sided check"
    assert "total_fee=None" in src, "fee+vat must not be summed — the relationship is unknown"


# ─────────────────────────── failure must not look like an empty book ───────────────────────────


@pytest.mark.asyncio
async def test_an_UNREADABLE_envelope_RAISES_rather_than_reading_as_no_fills() -> None:
    """Inherited from parse_order_items, and it matters more here, not less.

    In the reconciler an empty book marks live orders terminal. Here it would silently bank
    nothing and report a clean run — losing a day of data that cannot be recovered.
    """
    reader = _Reader({"unexpected": "shape"})
    with pytest.raises(LiberatorTransportError, match="refusing to report this as an empty"):
        await sweep_account(reader, FakePool(FakeConn()), account=_ACCOUNT, now=_NOW)


@pytest.mark.asyncio
async def test_one_account_failing_does_not_lose_the_others() -> None:
    """The data is perishable: account A's transport error is no reason to lose account B."""
    reader = _Reader(RuntimeError("bridge down"), _envelope(_row(orderNo="B1")))
    conn = FakeConn(fetch_results=[[]], fetchval_results=[1])
    res = await sweep_accounts(reader, FakePool(conn), accounts=["70000002", _ACCOUNT], now=_NOW)
    assert res.banked == 1, "the second account must still be swept"
    assert len(res.errors) == 1 and "bridge down" in res.errors[0]


@pytest.mark.asyncio
async def test_one_bad_ROW_does_not_lose_the_rest_of_the_day() -> None:
    """A row that fails to insert must not abort the sweep — the others are still perishable."""
    conn = FakeConn(fetch_results=[[]], fetchval_results=[RuntimeError("boom"), 2])

    async def fetchval(sql: str, *args: Any) -> Any:
        conn.calls.append(("fetchval", sql, args))
        v = conn.fetchval_results.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    conn.fetchval = fetchval  # type: ignore[method-assign]
    reader = _Reader(_envelope(_row(orderNo="A"), _row(orderNo="B")))
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)
    assert res.banked == 1 and len(res.errors) == 1


@pytest.mark.asyncio
async def test_errors_are_reported_and_a_run_with_errors_is_not_clean() -> None:
    """A returned SweepResult is not proof of a clean run; the caller must read .errors."""
    reader = _Reader(RuntimeError("down"))
    res = await sweep_accounts(reader, FakePool(FakeConn()), accounts=[_ACCOUNT], now=_NOW)
    assert res.errors and res.banked == 0
    assert res.semantics_are_now_answerable is False


# ─────────────────────────── shape and placement ───────────────────────────


@pytest.mark.asyncio
async def test_it_reads_the_SAME_path_the_reconciler_already_polls() -> None:
    """No new venue surface: the sweep must reuse ``orders_path``, not invent a route.

    A second route would be a new venue call to justify under the anti-polling rule; this is
    the read the platform already makes every 12 s, taken once.
    """
    reader = _Reader(_envelope())
    await sweep_account(reader, FakePool(FakeConn()), account=_ACCOUNT, now=_NOW)
    assert reader.paths == [f"orders/{_ACCOUNT}"]


def test_the_sweep_needs_no_PIN() -> None:
    """A read has no business holding a trading credential ([[TK-0529]])."""
    src = Path("src/quant_execution_engine/observations/blotter_sweep.py").read_text("utf-8")
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#") and '"""' not in ln
    )
    assert "get_secret_value" not in code, "no credential is unwrapped here"
    assert "pin=" not in code, "the sweep must never stamp a PIN"
    assert "liberator_pin" not in code, "it must not even read the PIN setting"
    # positive control: the filter keeps real code, so the three assertions above are not
    # passing merely because `code` came out empty.
    assert "get_json" in code


def test_the_sweep_is_NOT_REACHABLE_from_the_order_path() -> None:
    """Cost capture must never be able to delay or fail order reconciliation.

    This is why the sweep is a separate read rather than a hook inside the 12 s reconcile
    loop: a DB write in that loop could stall order recovery on a real-money node.
    """
    import ast

    root = Path("src/quant_execution_engine")
    for mod in (
        root / "core" / "router.py",
        root / "adapters" / "liberator" / "adapter.py",
        root / "adapters" / "liberator" / "reconciler.py",
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
                assert "blotter_sweep" not in n and "observations" not in n, (
                    f"{mod.name} imports {n} — cost capture is now in the order path"
                )


def test_sweep_result_counters_start_at_zero_and_are_per_run() -> None:
    """A shared mutable default would accumulate across runs and overstate a day's capture."""
    a, b = SweepResult(), SweepResult()
    a.errors.append("x")
    assert b.errors == [], "errors must not be a shared default"
    assert inspect.signature(sweep_accounts).parameters["accounts"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert blotter_sweep.SOURCE.startswith("liberator blotter")


@pytest.mark.asyncio
async def test_an_UNPARSEABLE_fee_is_counted_SEPARATELY_from_an_absent_one() -> None:
    """🔑 Three cases, not two. Malformed is not the same claim as absent.

    A field the venue SENT but we could not read means a wire or parser change; recording it
    as "not reported" hides that behind a NULL that looks routine. The raw payload is banked
    either way, so nothing is lost — but only the counter makes anyone go and look.
    """
    absent = _row(orderNo="ABSENT")
    absent.pop("fee")
    reader = _Reader(_envelope(_row(orderNo="BAD", fee="not-a-number"), absent, _row(orderNo="OK")))
    conn = FakeConn(fetch_results=[[]], fetchval_results=[1, 2, 3])
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)

    assert res.malformed_values == 1, "only the unreadable one counts as malformed"
    assert res.rows_with_a_fee_value == 1, "neither malformed nor absent is a reading"
    commissions = [c[2][11] for c in conn.calls if c[0] == "fetchval"]
    assert commissions == [None, None, Decimal("28.00")], "malformed banks NULL, not a guess"
    assert res.banked == 3, "an unreadable field must not lose the row — raw is kept verbatim"


@pytest.mark.asyncio
async def test_an_unreadable_MATCHED_count_skips_the_row_rather_than_inventing_a_quantity() -> None:
    """Erring toward skipping loses one row of a perishable day; erring toward banking puts a
    fabricated quantity into the cost record permanently. The reversible failure wins."""
    reader = _Reader(_envelope(_row(orderNo="BADQTY", matched="???")))
    conn = FakeConn(fetch_results=[[]])
    res = await sweep_account(reader, FakePool(conn), account=_ACCOUNT, now=_NOW)
    assert res.filled_rows == 0 and res.banked == 0
    assert not any(c[0] == "fetchval" for c in conn.calls), "nothing may be inserted"


def test_Money_keeps_zero_absent_and_malformed_apart() -> None:
    """The type directly, because the three cases are the module's core claim."""
    assert _money("28.00") == Money(Decimal("28.00"), False)
    assert _money("0") == Money(Decimal("0"), False)
    assert _money("1,234.50") == Money(Decimal("1234.50"), False), "venue commas"
    assert _money(None) == Money(None, False), "absent"
    assert _money("   ") == Money(None, False), "blank-padded is absent, not malformed"
    assert _money("abc") == Money(None, True), "malformed"
    assert _money("0").value == Decimal("0") and _money(None).value is None
    assert _money("0") != _money(None), "a real zero must never equal an absent field"

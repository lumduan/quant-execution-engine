"""🔒 The ONE-SIDED check, dated resolution, and the rare upward path.

The upward path may never run in production: the operator has never seen an observation
exceed the basis, and the basis is pinned at the most expensive tier. **A branch that never
runs is a branch nobody knows is broken**, so it is driven here with a synthetic observation.
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from src.quant_execution_engine.reference.fee_schedule import (
    Verdict,
    append_entry,
    load_fee_schedule,
    render_entry,
)

_CANON = Path("src/quant_execution_engine/reference/fee_schedule.toml")
_ON = date(2026, 9, 5)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A copy — adoption APPENDS to a file, and a test must never touch the real one."""
    dst = tmp_path / "fee_schedule.toml"
    shutil.copy(_CANON, dst)
    return dst


def test_an_observation_AT_OR_BELOW_the_basis_is_silent(scratch: Path) -> None:
    """The normal case: a promotion or a better tier. Record it, do not alert."""
    s = load_fee_schedule(scratch)
    for observed in ("8", "12", "14"):  # 14 == the basis: equal is NOT above
        c = s.compare("s50_futures", "commission", Decimal(observed), on=_ON)
        assert c.verdict is Verdict.AT_OR_BELOW, observed
        assert c.should_alert is False and c.should_adopt is False, observed


def test_an_observation_ABOVE_the_basis_ALERTS(scratch: Path) -> None:
    """🔑 The rare path. The conservative basis has stopped being conservative."""
    s = load_fee_schedule(scratch)
    c = s.compare("s50_futures", "commission", Decimal("16.50"), on=_ON)
    assert c.verdict is Verdict.ABOVE
    assert c.should_alert is True and c.should_adopt is True
    assert c.basis == Decimal("14") and c.observed == Decimal("16.50")


def test_the_check_is_ASYMMETRIC_not_a_tolerance_band(scratch: Path) -> None:
    """🔴 A symmetric check would alert on a promotion — the safe direction.

    Direction matters more than magnitude here: a basis that is too expensive makes a
    strategy look worse than it is; too cheap makes it look better, and in this umbrella has
    already turned a losing result into an apparently positive one. A large move DOWN must
    stay silent while a small move UP must fire.
    """
    s = load_fee_schedule(scratch)
    big_drop = s.compare("s50_futures", "commission", Decimal("1"), on=_ON)
    tiny_rise = s.compare("s50_futures", "commission", Decimal("14.01"), on=_ON)
    assert big_drop.should_alert is False, "a 93% drop must not alert"
    assert tiny_rise.should_alert is True, "a 0.07% rise must alert"


def test_adoption_APPENDS_and_the_PRIOR_ENTRY_SURVIVES(scratch: Path) -> None:
    """🔑 Overwriting would make results before and after silently incomparable.

    This project has just spent a week on a study whose conclusion rested on a cost basis
    nobody could reconstruct. The prior entry staying resolvable is what prevents a repeat.
    """
    s = load_fee_schedule(scratch)
    before = s.series_for("s50_futures", "commission")
    assert len(before) == 1 and before[0].value == Decimal("14")

    c = s.compare("s50_futures", "commission", Decimal("16.50"), on=_ON)
    append_entry(
        scratch,
        render_entry(
            "s50_futures",
            "commission",
            value=c.observed,
            unit="THB per contract per side",
            effective_from=date(2026, 9, 10),
            observed_on=_ON,
            prior=c.basis,
        ),
    )
    after = load_fee_schedule(scratch).series_for("s50_futures", "commission")
    assert len(after) == 2, "adoption must APPEND"
    assert after[0].value == Decimal("14"), "the PRIOR entry was lost"
    assert after[0].effective_from == date(2026, 9, 4)
    assert after[1].value == Decimal("16.50")
    assert after[1].source_kind == "observed_adopted"


def test_resolution_returns_the_RIGHT_entry_on_EACH_SIDE_of_the_boundary(
    scratch: Path,
) -> None:
    """🔑 The property that makes an old result reconstructible.

    Asserted on BOTH sides plus the boundary day itself — a resolver that always returned the
    newest entry would pass a one-sided check and silently rewrite history.
    """
    s0 = load_fee_schedule(scratch)
    c = s0.compare("s50_futures", "commission", Decimal("16.50"), on=_ON)
    append_entry(
        scratch,
        render_entry(
            "s50_futures",
            "commission",
            value=c.observed,
            unit="THB per contract per side",
            effective_from=date(2026, 9, 10),
            observed_on=_ON,
            prior=c.basis,
        ),
    )
    s = load_fee_schedule(scratch)

    def in_force_on(day: date) -> Decimal:
        return s.resolve("s50_futures", "commission", on=day, allow_stale=True).value

    assert in_force_on(date(2026, 9, 9)) == Decimal("14"), "the day BEFORE: still the old basis"
    assert in_force_on(date(2026, 9, 10)) == Decimal("16.50"), "the boundary day: the new basis"
    assert in_force_on(date(2026, 9, 11)) == Decimal("16.50"), "and after"


def test_resolve_REQUIRES_a_date_rather_than_defaulting_to_today() -> None:
    """A default of "now" would hand an August calculation September's basis, silently."""
    import inspect

    sig = inspect.signature(load_fee_schedule().resolve)
    assert sig.parameters["on"].default is inspect.Parameter.empty


def test_a_date_before_every_entry_RAISES_rather_than_guessing(scratch: Path) -> None:
    """There is no basis for a period the schedule does not cover. Say so."""
    s = load_fee_schedule(scratch)
    with pytest.raises(KeyError, match="no entry effective on or before"):
        s.resolve("s50_futures", "commission", on=date(2020, 1, 1), allow_stale=True)


def test_compare_reads_a_STALE_basis_deliberately(scratch: Path) -> None:
    """⚠️ The one place allow_stale defaults True, and it must.

    A stale basis is exactly the condition this check exists to detect. Refusing to read it
    would disable the alarm precisely when it matters most.
    """
    import inspect

    s = load_fee_schedule(scratch)
    assert inspect.signature(s.compare).parameters["allow_stale"].default is True
    far = datetime(2030, 1, 1, tzinfo=UTC)
    c = s.compare("s50_futures", "commission", Decimal("99"), on=far.date())
    assert c.should_alert is True


def test_the_probe_is_NOT_REACHABLE_from_the_order_path() -> None:
    """🔴 Structural, not a comment. A cost path that can fail on a network call is not one.

    Walks the real import graph: no module reachable from the router or the adapters may
    import the probe definition. A docstring saying "not in the order path" guarantees
    nothing; this fails if someone wires it in.
    """
    import ast

    root = Path("src/quant_execution_engine")
    order_path = [
        root / "core" / "router.py",
        root / "adapters" / "liberator" / "adapter.py",
        root / "adapters" / "streaming_pro" / "adapter.py",
    ]
    for mod in order_path:
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert "probe" not in n, f"{mod.name} imports {n} — the probe is in the order path"


def test_no_fabricated_docstring_literals_are_used_as_evidence() -> None:
    """⚠️ TK-0524 flagged the bridge's example response as FABRICATED.

    `"Commission Fee + VAT": "17.12"` is a docstring literal, and 17.12 = 16 x 1.07
    contradicts the 14 tier. It is exactly the shape of thing that gets cited as a finding,
    so this asserts it never entered our schedule or fixtures.
    """
    for p in (_CANON, Path("src/quant_execution_engine/reference/probe_order.toml")):
        text = p.read_text(encoding="utf-8")
        assert "17.12" not in text, f"{p.name} carries a fabricated literal"
        assert "6.53" not in text, f"{p.name} carries a fabricated literal"

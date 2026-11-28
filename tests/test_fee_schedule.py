"""🔒 The canonical fee schedule: provenance, Decimal exactness, and REFUSED stale reads.

The schedule exists because five independent cost models disagreed and none matched what the
operator pays. These tests defend the properties that make it worth trusting — not merely
that it parses.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from src.quant_execution_engine.reference import fee_schedule as mod
from src.quant_execution_engine.reference.fee_schedule import (
    FeeScheduleStale,
    load_fee_schedule,
)

_NOW = datetime(2026, 9, 4, tzinfo=UTC)
# The SET-equities entries were recorded 2026-11-26; read them inside their window.
_NOW_EQ = datetime(2026, 11, 26, 13, tzinfo=UTC)


def test_the_all_in_arithmetic_is_EXACT_in_Decimal() -> None:
    """🔑 (14 + 6) × 1.07 == 21.40, exactly.

    Pinned so that editing any component without the all-in figure fails. In float this is
    21.400000000000002 and the equality silently breaks — which is why every value in the
    TOML is a quoted string converted with ``Decimal(str)``.
    """
    s = load_fee_schedule()
    for inst in ("s50_futures", "s50_options"):
        c = s.get(inst, "commission", now=_NOW).value
        x = s.get(inst, "exchange_clearing", now=_NOW).value
        v = s.get(inst, "vat_rate", now=_NOW).value
        allin = s.get(inst, "all_in_per_side", now=_NOW).value
        assert (c + x) * (Decimal("1") + v) == allin == Decimal("21.40"), inst


def test_every_entry_carries_provenance_and_a_staleness_rule() -> None:
    """No entry may exist without saying where it came from and when it expires."""
    s = load_fee_schedule()
    for inst in s.instruments.values():
        for key, entries in inst.series.items():
            assert entries, f"{key} has an EMPTY series"
            for e in entries:
                assert e.source.strip(), f"{key} has no source"
                assert e.source_kind in {
                    "operator_supplied",
                    "venue_fetched",
                    "broker_published",
                    "derived",
                    "observed_adopted",
                }
                assert e.max_age_days > 0, f"{key} has no staleness rule"
                assert e.recorded_utc.tzinfo is not None, f"{key} recorded_utc is naive"
                # Every entry must say WHEN it takes effect — the property that keeps an
                # old result reconstructible after an adoption appends.
                assert e.effective_from is not None, f"{key} has no effective_from"


def test_operator_supplied_figures_are_LABELLED_as_uncorroborated() -> None:
    """🔴 The fee components are the operator's word and nothing else confirms them.

    If this ever flips to ``corroborated: true`` it must be because a source was found and
    recorded — not because the figure started to feel established.
    """
    s = load_fee_schedule()
    for inst in ("s50_futures", "s50_options"):
        for field in ("commission", "exchange_clearing", "vat_rate"):
            e = s.get(inst, field, now=_NOW)
            assert e.source_kind == "operator_supplied", f"{inst}.{field}"
            assert e.corroborated is False, f"{inst}.{field} claims corroboration it lacks"


def test_venue_fetched_figures_carry_a_VERBATIM_quote_and_a_url() -> None:
    """A 'fetched' figure with no quote is indistinguishable from a remembered one.

    Binds a broker's published schedule the same way as a venue's page (2026-11-26).
    """
    s = load_fee_schedule()
    for inst in s.instruments.values():
        for key, entries in inst.series.items():
            for e in entries:
                if e.source_kind in ("venue_fetched", "broker_published"):
                    assert e.source.startswith("https://"), f"{key}: source is not a URL"
                    assert e.verbatim, f"{key}: venue-fetched but quotes nothing"


def test_the_venue_multiplier_and_tick_match_their_verbatim_quotes() -> None:
    """The parsed value must agree with the text it claims to come from.

    Guards the failure where someone edits the number and leaves the quote — at which point
    the provenance is worse than absent, because it looks checked.
    """
    s = load_fee_schedule()
    m = s.get("s50_futures", "contract_multiplier", now=_NOW)
    assert m.value == Decimal("200") and "THB 200 per index point" in m.verbatim
    t = s.get("s50_futures", "tick_size", now=_NOW)
    assert t.value == Decimal("0.1") and "0.1 index point" in t.verbatim
    cap = s.get("s50_options", "venue_exchange_fee_cap", now=_NOW)
    assert cap.value == Decimal("5") and "Maximum of THB 5" in cap.verbatim


def test_a_STALE_entry_RAISES_and_the_message_says_what_to_do() -> None:
    """🔑 The property the brief names: a stale figure must not come back quietly."""
    s = load_fee_schedule()
    future = _NOW + timedelta(days=10_000)
    with pytest.raises(FeeScheduleStale) as exc:
        s.get("s50_futures", "commission", now=future)
    msg = str(exc.value)
    assert "past its" in msg and "allow_stale" in msg and "commission" in msg


def test_allow_stale_returns_it_but_must_be_asked_for_EXPLICITLY() -> None:
    """The opt-out exists, and it is a marker a reviewer can see — not a default."""
    s = load_fee_schedule()
    future = _NOW + timedelta(days=10_000)
    e = s.get("s50_futures", "commission", now=future, allow_stale=True)
    assert e.value == Decimal("14")
    assert inspect.signature(s.get).parameters["allow_stale"].default is False


def test_a_FRESH_entry_is_unaffected() -> None:
    """Positive control — a guard that refuses everything is not a guard.

    Without this, `is_stale` could return True unconditionally and every test above would
    still pass.
    """
    s = load_fee_schedule()
    e = s.get("s50_futures", "commission", now=_NOW)
    assert e.value == Decimal("14") and not e.is_stale(now=_NOW)


def test_an_UNQUOTED_number_in_the_toml_is_REFUSED(tmp_path: Path) -> None:
    """🔴 A bare TOML number is a float, and a float fee is a wrong number nobody sees.

    The loader refuses it rather than silently accepting the precision loss.
    """
    bad = tmp_path / "bad.toml"
    bad.write_text(
        'schema_version = 1\n[meta]\ncurrency = "THB"\n'
        '[instrument.x]\nlabel = "x"\nvenue = "v"\nticker = "t"\nbroker = "b"\n'
        '[[instrument.x.commission]]\nvalue = 14.0\nunit = "u"\n'
        'effective_from = "2026-09-04"\n'
        'source_kind = "operator_supplied"\nsource = "s"\ncorroborated = false\n'
        'recorded_utc = "2026-09-04T00:00:00Z"\nmax_age_days = 1\n',
        encoding="utf-8",
    )
    with pytest.raises(TypeError, match="quoted STRING"):
        load_fee_schedule(bad)


def test_the_loader_exposes_no_cost_LOGIC() -> None:
    """🔑 Facts, not decisions — asserted, not merely intended.

    The schedule must never grow a `round_trip_cost()`. The moment it does it stops being a
    fact source and becomes a sixth cost model, which is what this work exists to prevent.
    """
    banned = ("cost", "slippage", "pnl", "round_trip", "total_fee", "charge")
    for name, obj in vars(mod).items():
        if name.startswith("_") or not callable(obj):
            continue
        assert not any(b in name.lower() for b in banned), (
            f"{name}() looks like cost LOGIC; this module carries facts only"
        )


def test_the_gaps_are_recorded_rather_than_filled() -> None:
    """SSF must stay OUT of the canonical entries.

    SET equities LEFT the gaps on 2026-11-26, when both of the gap's own exit criteria were met
    (the broker's published schedule, and a billed actual reconciling to it). A gap leaves this
    set only by such a recorded closure, never by an edit to this line alone.
    """
    s = load_fee_schedule()
    assert {g.instrument for g in s.gaps} == {"single_stock_futures"}
    assert "single_stock_futures" not in s.instruments
    assert "set_equities" in s.instruments
    for g in s.gaps:
        assert g.what_would_close_it.strip(), f"{g.instrument}: gap with no exit criterion"


def test_asking_for_a_gap_says_so_rather_than_raising_a_bare_KeyError() -> None:
    """A consumer reaching for SSF should learn WHY it is absent, not just that it is."""
    s = load_fee_schedule()
    with pytest.raises(KeyError, match="deliberately NOT canonical"):
        s.get("single_stock_futures", "commission", now=_NOW)


# ---- SET equities: closed from a gap 2026-11-26 (Liberator Announcement 016/2568, #452) ------


def test_set_equities_all_in_is_EXACT_in_Decimal() -> None:
    """(0.000499 + 0.00007) x 1.07 == 0.00060883, exactly, so editing a component fails."""
    s = load_fee_schedule()
    on = date(2026, 11, 26)
    c = s.resolve("set_equities", "commission", on=on, now=_NOW_EQ).value
    o = s.resolve("set_equities", "other_fees", on=on, now=_NOW_EQ).value
    v = s.resolve("set_equities", "vat_rate", on=on, now=_NOW_EQ).value
    allin = s.resolve("set_equities", "all_in_per_side", on=on, now=_NOW_EQ).value
    assert (c + o) * (Decimal("1") + v) == allin == Decimal("0.00060883")


def test_set_equities_values_match_the_broker_quotes_they_cite() -> None:
    """The first tier is pinned, and each parsed value agrees with its verbatim percentage."""
    s = load_fee_schedule()
    on = date(2026, 11, 26)
    c = s.resolve("set_equities", "commission", on=on, now=_NOW_EQ)
    o = s.resolve("set_equities", "other_fees", on=on, now=_NOW_EQ)
    assert c.source_kind == o.source_kind == "broker_published"
    assert c.value == Decimal("0.0499") / 100 and "0.0499%" in c.verbatim
    assert "ไม่เกิน 1,000,000" in c.verbatim, "the basis must be the FIRST tier"
    assert o.value == Decimal("0.007") / 100 and "0.007%" in o.verbatim


def test_set_equities_reproduces_the_billed_SPALI_confirmation() -> None:
    """The basis reproduces a real billed fill to the satang (operator-supplied, 2026-11-26).

    SPALI BUY 1,000 @ 14.55: commission 7.26, fee 1.02, VAT 0.58, net 14,558.86.
    """
    s = load_fee_schedule()
    on = date(2026, 11, 26)
    amount = Decimal("1000") * Decimal("14.55")
    c = s.resolve("set_equities", "commission", on=on, now=_NOW_EQ).value
    o = s.resolve("set_equities", "other_fees", on=on, now=_NOW_EQ).value
    v = s.resolve("set_equities", "vat_rate", on=on, now=_NOW_EQ).value
    commission = (amount * c).quantize(Decimal("0.01"))
    fee = (amount * o).quantize(Decimal("0.01"))
    vat = ((commission + fee) * v).quantize(Decimal("0.01"))
    assert (commission, fee, vat) == (Decimal("7.26"), Decimal("1.02"), Decimal("0.58"))
    assert amount + commission + fee + vat == Decimal("14558.86")


def test_set_equities_has_no_entry_before_the_schedule_took_effect() -> None:
    """Before 2025-10-01 the rate was another announcement's, unknown here: refuse, not guess."""
    s = load_fee_schedule()
    with pytest.raises(LookupError, match="no entry effective on or before"):
        s.resolve("set_equities", "commission", on=date(2025, 9, 30), now=_NOW_EQ)

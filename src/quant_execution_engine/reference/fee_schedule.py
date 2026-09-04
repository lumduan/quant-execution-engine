"""Typed reader for the canonical fee BASIS — Decimal end-to-end, stale reads REFUSED.

🔴 **This file reads a POLICY, not a record of facts.** The broker runs promotions, so the
rate actually charged varies day to day. The TOML is a fixed, deliberately conservative basis
pinned at the most expensive tier so every strategy calculation is deterministic and never
flatters itself. What is *actually* charged lives in ``execution.fee_observations``.

🔴 **Entries are an APPEND-ONLY DATED SERIES.** :meth:`FeeSchedule.resolve` returns the entry
in force **on a date the caller must supply** — never "the newest". Adoption of a higher
observed rate appends; it never overwrites. Overwriting would silently make results computed
before and after incomparable.

🔴 **This module exposes the BASIS, never COST LOGIC.** It does not combine fees into a cost,
model slippage, or condition anything on a strategy or an outcome. A test fails if a
combining function appears.

🔴 **A STALE FIGURE IS REFUSED, NOT RETURNED** — same posture as TK-0488: refuse rather than
substitute. Silent staleness is how a wrong session time survived a week of briefs here.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

_SCHEDULE_PATH: Final[Path] = Path(__file__).with_name("fee_schedule.toml")

_SOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {"operator_supplied", "venue_fetched", "derived", "observed_adopted"}
)


class FeeScheduleStale(RuntimeError):
    """A figure was read after its staleness window. Never raised for a fresh entry."""


class Verdict(StrEnum):
    """Outcome of the ONE-SIDED comparison. The asymmetry is the whole point."""

    AT_OR_BELOW = "at_or_below"
    """observed <= basis. A promotion or a better tier. Record it; do NOT alert."""

    ABOVE = "above"
    """observed > basis. The conservative basis is no longer conservative: every strategy
    calculation is UNDERSTATING cost. Alert, and adopt the observed figure going forward."""


@dataclass(frozen=True)
class FeeEntry:
    """One dated basis entry, with the provenance that makes it usable."""

    key: str
    value: Decimal
    unit: str
    source_kind: str
    source: str
    corroborated: bool
    effective_from: date
    recorded_utc: datetime
    max_age_days: int
    note: str = ""
    verbatim: str = ""

    def age_days(self, *, now: datetime | None = None) -> int:
        return ((now or datetime.now(UTC)) - self.recorded_utc).days

    def is_stale(self, *, now: datetime | None = None) -> bool:
        return self.age_days(now=now) > self.max_age_days


@dataclass(frozen=True)
class Instrument:
    key: str
    label: str
    venue: str
    ticker: str
    broker: str
    series: dict[str, tuple[FeeEntry, ...]]
    """field -> append-only series, oldest first. Never a single value."""


@dataclass(frozen=True)
class Gap:
    """A class deliberately NOT made canonical, and what would change that."""

    instrument: str
    label: str
    what_exists: str
    where: str
    why_not_canonical: str
    what_would_close_it: str


@dataclass(frozen=True)
class Comparison:
    """The result of checking one observation against the basis in force."""

    instrument: str
    field: str
    observed: Decimal
    basis: Decimal
    basis_effective_from: date
    verdict: Verdict

    @property
    def should_alert(self) -> bool:
        """🔴 ONE-SIDED. Only an observation ABOVE the basis alerts.

        Direction matters more than magnitude. A basis that is too expensive makes a strategy
        look worse than it is — safe. Too cheap makes it look better, and in this umbrella has
        already turned a losing result into an apparently positive one.
        """
        return self.verdict is Verdict.ABOVE

    @property
    def should_adopt(self) -> bool:
        """Adoption APPENDS a new dated entry. It never overwrites the prior one."""
        return self.verdict is Verdict.ABOVE


@dataclass(frozen=True)
class FeeSchedule:
    schema_version: int
    currency: str
    instruments: dict[str, Instrument]
    gaps: tuple[Gap, ...]

    def series_for(self, instrument: str, field: str) -> tuple[FeeEntry, ...]:
        """The whole append-only series for one field, oldest first."""
        try:
            inst = self.instruments[instrument]
        except KeyError:
            raise KeyError(
                f"no instrument {instrument!r} in the fee schedule "
                f"(have: {sorted(self.instruments)}); if it is one of the recorded gaps "
                f"({[g.instrument for g in self.gaps]}), its facts are deliberately NOT "
                "canonical yet"
            ) from None
        try:
            return inst.series[field]
        except KeyError:
            raise KeyError(
                f"{instrument!r} has no field {field!r} (have: {sorted(inst.series)})"
            ) from None

    def resolve(
        self,
        instrument: str,
        field: str,
        *,
        on: date,
        allow_stale: bool = False,
        now: datetime | None = None,
    ) -> FeeEntry:
        """The entry IN FORCE on ``on`` — not the newest one.

        🔴 **``on`` is required and has no default.** A caller computing over August must get
        August's basis; defaulting to "today" would hand them September's, which is exactly
        how a study ends up with a conclusion nobody can reconstruct. Making the date
        mandatory forces the caller to state the period they are computing over.
        """
        entries = self.series_for(instrument, field)
        applicable = [e for e in entries if e.effective_from <= on]
        if not applicable:
            raise KeyError(
                f"{instrument}.{field} has no entry effective on or before {on}; the "
                f"earliest is {entries[0].effective_from}"
            )
        entry = applicable[-1]
        if entry.is_stale(now=now) and not allow_stale:
            raise FeeScheduleStale(
                f"{instrument}.{field} was recorded {entry.age_days(now=now)} days ago "
                f"({entry.recorded_utc:%Y-%m-%d}), past its {entry.max_age_days}-day limit. "
                f"Re-fetch from {entry.source!r}, or pass allow_stale=True to accept a "
                "figure known to be out of date."
            )
        return entry

    def get(
        self,
        instrument: str,
        field: str,
        *,
        allow_stale: bool = False,
        now: datetime | None = None,
    ) -> FeeEntry:
        """The entry in force TODAY. Convenience wrapper over :meth:`resolve`.

        ⚠️ Prefer ``resolve(..., on=<the date you are computing over>)``. This shortcut is
        correct only when "now" really is the period of interest; for anything historical it
        hands back a basis that was not in force at the time.
        """
        return self.resolve(
            instrument,
            field,
            on=(now or datetime.now(UTC)).date(),
            allow_stale=allow_stale,
            now=now,
        )

    def compare(
        self,
        instrument: str,
        field: str,
        observed: Decimal,
        *,
        on: date,
        allow_stale: bool = True,
    ) -> Comparison:
        """Check one observation against the basis in force. **Deliberately asymmetric.**

        ``allow_stale`` defaults to **True** here on purpose, and it is the one place it
        does: a stale basis is exactly the condition this check exists to detect, so refusing
        to read it would disable the alarm precisely when it matters most.
        """
        entry = self.resolve(instrument, field, on=on, allow_stale=allow_stale)
        verdict = Verdict.ABOVE if observed > entry.value else Verdict.AT_OR_BELOW
        return Comparison(
            instrument=instrument,
            field=field,
            observed=observed,
            basis=entry.value,
            basis_effective_from=entry.effective_from,
            verdict=verdict,
        )


def render_entry(
    instrument: str,
    field: str,
    *,
    value: Decimal,
    unit: str,
    effective_from: date,
    observed_on: date,
    prior: Decimal,
) -> str:
    """The TOML block for an adopted observation. Pure — returns text, writes nothing."""
    return f"""
  [[instrument.{instrument}.{field}]]
  value        = "{value}"
  effective_from = "{effective_from.isoformat()}"
  unit         = "{unit}"
  source_kind  = "observed_adopted"
  source       = "execution.fee_observations, observed {observed_on.isoformat()}"
  corroborated = true
  note = \"\"\"
🔴 ADOPTED AUTOMATICALLY because an observation EXCEEDED the prior basis of {prior}.
The conservative basis had stopped being conservative, which means every strategy
calculation between {effective_from.isoformat()} and this entry was UNDERSTATING cost.
The prior entry is retained above and stays resolvable for any period before
{effective_from.isoformat()} — results computed then must remain reconstructible.\"\"\"
  recorded_utc = "{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}"
  max_age_days = 180
"""


def append_entry(path: Path, block: str) -> None:
    """APPEND a rendered entry. Never rewrites or removes anything already present.

    Deliberately dumb: it opens in append mode, so there is no code path here that could
    delete a prior entry even if it were called wrongly.
    """
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)


def _entry(key: str, raw: dict[str, Any]) -> FeeEntry:
    kind = str(raw["source_kind"])
    if kind not in _SOURCE_KINDS:
        raise ValueError(f"{key}: source_kind {kind!r} not in {sorted(_SOURCE_KINDS)}")
    value = raw["value"]
    if not isinstance(value, str):
        raise TypeError(
            f"{key}: value must be a quoted STRING in the TOML (got {type(value).__name__}). "
            "Unquoted numbers become floats and a float fee is a wrong number nobody sees."
        )
    return FeeEntry(
        key=key,
        value=Decimal(value),
        unit=str(raw["unit"]),
        source_kind=kind,
        source=str(raw["source"]),
        corroborated=bool(raw["corroborated"]),
        effective_from=date.fromisoformat(str(raw["effective_from"])),
        recorded_utc=datetime.fromisoformat(str(raw["recorded_utc"]).replace("Z", "+00:00")),
        max_age_days=int(raw["max_age_days"]),
        note=str(raw.get("note", "")).strip(),
        verbatim=str(raw.get("verbatim", "")).strip(),
    )


def load_fee_schedule(path: Path | None = None) -> FeeSchedule:
    """Parse the canonical TOML. Raises on any entry missing provenance."""
    src = path or _SCHEDULE_PATH
    with src.open("rb") as fh:
        raw = tomllib.load(fh)
    instruments: dict[str, Instrument] = {}
    for key, block in raw["instrument"].items():
        series: dict[str, tuple[FeeEntry, ...]] = {}
        for fk, fv in block.items():
            if not isinstance(fv, list):
                continue
            parsed = [_entry(f"{key}.{fk}", e) for e in fv]
            # Sorted so `resolve` can take the last applicable entry, and so a block
            # appended out of order still resolves correctly.
            series[fk] = tuple(sorted(parsed, key=lambda e: e.effective_from))
        instruments[key] = Instrument(
            key=key,
            label=str(block["label"]),
            venue=str(block["venue"]),
            ticker=str(block["ticker"]),
            broker=str(block["broker"]),
            series=series,
        )
    gaps = tuple(
        Gap(
            instrument=str(g["instrument"]),
            label=str(g["label"]),
            what_exists=str(g["what_exists"]).strip(),
            where=str(g["where"]).strip(),
            why_not_canonical=str(g["why_not_canonical"]).strip(),
            what_would_close_it=str(g["what_would_close_it"]).strip(),
        )
        for g in raw.get("gap", [])
    )
    return FeeSchedule(
        schema_version=int(raw["schema_version"]),
        currency=str(raw["meta"]["currency"]),
        instruments=instruments,
        gaps=gaps,
    )

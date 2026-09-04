"""Typed reader for the canonical fee schedule — Decimal end-to-end, stale reads REFUSED.

🔴 **This module exposes FACTS, never COST LOGIC.** It returns fees, multipliers and tick
sizes. It does not combine them into a cost, model slippage, or condition anything on a
strategy or an outcome — consumers keep their own logic and read the facts from here. A test
(`test_the_loader_exposes_no_cost_LOGIC`) fails if a combining function appears.

🔴 **A STALE FIGURE IS REFUSED, NOT RETURNED.** Every entry carries `max_age_days`; reading
past it raises :class:`FeeScheduleStale` naming the entry, its age and its limit. A caller
that genuinely wants a known-stale value must pass ``allow_stale=True``, which then appears
in their code and in review.

Returning a stale figure quietly is the precise mechanism by which a wrong session time
survived a week of briefs in this project, and it is the same posture the engine took for
unanswerable broker reads in TK-0488: **refuse rather than substitute.**
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

_SCHEDULE_PATH: Final[Path] = Path(__file__).with_name("fee_schedule.toml")

# `derived` is neither operator-supplied nor fetched: it is computed from siblings in the
# same file, and the arithmetic is pinned by a test.
_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"operator_supplied", "venue_fetched", "derived"})


class FeeScheduleStale(RuntimeError):
    """A figure was read after its staleness window. Never raised for a fresh entry."""


@dataclass(frozen=True)
class FeeEntry:
    """One fact, with the provenance that makes it usable.

    ``corroborated`` is deliberately separate from ``source_kind``: an
    ``operator_supplied`` figure with no independent confirmation is a legitimate fact to
    hold, and a caller is entitled to know that is what it is holding.
    """

    key: str
    value: Decimal
    unit: str
    source_kind: str
    source: str
    corroborated: bool
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
    entries: dict[str, FeeEntry]


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
class FeeSchedule:
    schema_version: int
    currency: str
    instruments: dict[str, Instrument]
    gaps: tuple[Gap, ...]

    def get(
        self,
        instrument: str,
        field: str,
        *,
        allow_stale: bool = False,
        now: datetime | None = None,
    ) -> FeeEntry:
        """One fact. Raises :class:`FeeScheduleStale` past the window unless opted out.

        ``allow_stale`` is not a convenience — it is a marker. It exists so that a caller
        who has decided a known-stale figure is acceptable has to say so somewhere a
        reviewer will see it.
        """
        try:
            inst = self.instruments[instrument]
        except KeyError:
            raise KeyError(
                f"no instrument {instrument!r} in the fee schedule "
                f"(have: {sorted(self.instruments)}); "
                f"if it is one of the recorded gaps ({[g.instrument for g in self.gaps]}), "
                "its facts are deliberately NOT canonical yet"
            ) from None
        try:
            entry = inst.entries[field]
        except KeyError:
            raise KeyError(
                f"{instrument!r} has no field {field!r} (have: {sorted(inst.entries)})"
            ) from None
        if entry.is_stale(now=now) and not allow_stale:
            raise FeeScheduleStale(
                f"{instrument}.{field} was recorded {entry.age_days(now=now)} days ago "
                f"({entry.recorded_utc:%Y-%m-%d}), past its {entry.max_age_days}-day limit. "
                f"Re-fetch from {entry.source!r}, or pass allow_stale=True to accept a "
                "figure known to be out of date."
            )
        return entry


def _entry(key: str, raw: dict[str, Any]) -> FeeEntry:
    kind = str(raw["source_kind"])
    if kind not in _SOURCE_KINDS:
        raise ValueError(f"{key}: source_kind {kind!r} not in {sorted(_SOURCE_KINDS)}")
    # 🔴 Decimal(str) — never float. Every monetary value in the TOML is a quoted string
    # precisely so this conversion is exact; a bare TOML float would already have lost it.
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
        fields = {k: v for k, v in block.items() if isinstance(v, dict)}
        instruments[key] = Instrument(
            key=key,
            label=str(block["label"]),
            venue=str(block["venue"]),
            ticker=str(block["ticker"]),
            broker=str(block["broker"]),
            entries={fk: _entry(f"{key}.{fk}", fv) for fk, fv in fields.items()},
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

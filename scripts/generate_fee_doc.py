"""Render the umbrella's fee-schedule mirror FROM the canonical TOML. The only writer.

🔴 The generated document is never hand-edited. `tests/test_fee_schedule_drift.py` fails if
it diverges from the canonical file, because two hand-maintained copies of the same facts is
the exact defect this whole change exists to end.

    uv run python scripts/generate_fee_doc.py            # write
    uv run python scripts/generate_fee_doc.py --check    # exit 1 if the doc is out of date
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.quant_execution_engine.reference.fee_schedule import (  # noqa: E402
    FeeSchedule,
    load_fee_schedule,
)

_DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "fee-schedule.md"
_CANON = "quant-execution-engine/src/quant_execution_engine/reference/fee_schedule.toml"
_KIND = {
    "operator_supplied": "🗣️ operator-supplied",
    "venue_fetched": "🏛️ venue-fetched",
    "derived": "🧮 derived",
}


def render(s: FeeSchedule) -> str:
    L: list[str] = []
    a = L.append
    a("# Trading fee schedule — GENERATED, do not edit")
    a("")
    a("> 🔴 **This file is GENERATED. Every edit here is lost and a drift test will fail.**")
    a("> The single source of truth is")
    a(f"> [`{_CANON}`](../../{_CANON}).")
    a(">")
    a("> ```bash")
    a("> cd quant-execution-engine && uv run python scripts/generate_fee_doc.py")
    a("> ```")
    a("")
    a("This document carries **facts** — fees, multipliers, tick sizes. It carries **no cost")
    a("logic, no slippage model, and nothing conditioned on a strategy or an outcome**.")
    a("Consumers keep their own logic and read the facts from here.")
    a("")
    a(f"Currency **{s.currency}** · schema v{s.schema_version}")
    a("")
    a("## Why this exists")
    a("")
    a("An audit on 2026-09-04 found **five independent cost models** in this umbrella. Their")
    a("multipliers and tick sizes were venue-correct; their **fees were not**, and the")
    a("operator's real schedule existed only in conversation. The widest divergence, measured")
    a("rather than repeated:")
    a("")
    a("| source | S50, per side | vs actual |")
    a("|---|---|---|")
    a("| operator's actual, all-in | **21.40** | — |")
    a(
        "| `tfex-s50-multi-tf-swing/backtest/costs.py` | 160 + 1 = **161** | "
        "**7.52×** (11.43× on commission alone) |"
    )
    a("")
    a("## How to read provenance")
    a("")
    a("| marker | meaning |")
    a("|---|---|")
    a(
        "| 🗣️ operator-supplied | Stated by the operator. "
        "**No published source, no in-tree corroboration.** |"
    )
    a("| 🏛️ venue-fetched | Read from the venue's own published page, quoted verbatim below. |")
    a("| 🧮 derived | Computed from other entries in this file; the arithmetic is test-pinned. |")
    a("")
    a("**`corroborated: no` is not a defect** — it is the honest state of a figure only one")
    a("person has stated. It is recorded so nobody later mistakes it for a fetched fact, which")
    a("is precisely how the SSF coefficients in this tree came to be mis-described as *seeds*.")
    a("")
    a("🔴 **Stale figures are REFUSED, not returned.** Each entry carries a staleness window;")
    a("reading past it raises `FeeScheduleStale`. A caller wanting a known-stale figure must")
    a("pass `allow_stale=True`, so the decision appears in their code and in review.")
    for key, inst in s.instruments.items():
        a("")
        a(f"## {inst.label}")
        a("")
        a(f"`{key}` · venue **{inst.venue}** · ticker **{inst.ticker}** · broker **{inst.broker}**")
        a("")
        a("| field | value | unit | provenance | corroborated | recorded | max age |")
        a("|---|---|---|---|---|---|---|")
        for fk, e in inst.entries.items():
            a(
                f"| `{fk}` | **{e.value}** | {e.unit} | {_KIND[e.source_kind]} | "
                f"{'yes' if e.corroborated else '**no**'} | {e.recorded_utc:%Y-%m-%d} | "
                f"{e.max_age_days}d |"
            )
        for fk, e in inst.entries.items():
            if e.verbatim or e.note:
                a("")
                a(f"**`{fk}`** — {e.source}")
                if e.verbatim:
                    a("")
                    a(f"> venue, verbatim: `{e.verbatim}`")
                if e.note:
                    a("")
                    for line in e.note.splitlines():
                        a(f"> {line}" if line.strip() else ">")
    a("")
    a("## Gaps — recorded, deliberately NOT filled")
    a("")
    a("These classes are modelled elsewhere in the umbrella but their fee facts do **not**")
    a("reach the standard above. Nothing uncorroborated is made canonical; the gap is named")
    a("instead, with what would close it.")
    for g in s.gaps:
        a("")
        a(f"### {g.label} (`{g.instrument}`)")
        a("")
        a(f"- **what exists:** `{g.what_exists}`")
        a(f"- **where:** `{g.where}`")
        a(f"- **why not canonical:** {g.why_not_canonical}")
        a(f"- **what would close it:** {g.what_would_close_it}")
    a("")
    a("---")
    a("")
    a(f"<sub>Generated from `{_CANON}` by `scripts/generate_fee_doc.py`. Do not edit.</sub>")
    a("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if the doc is out of date")
    ap.add_argument("--out", type=Path, default=_DOC)
    args = ap.parse_args()
    want = render(load_fee_schedule())
    if args.check:
        have = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if have != want:
            print(
                f"DRIFT: {args.out} does not match the canonical schedule.\n"
                "  regenerate: cd quant-execution-engine && "
                "uv run python scripts/generate_fee_doc.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {args.out} matches the canonical schedule.")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(want, encoding="utf-8")
    print(f"wrote {args.out} at {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

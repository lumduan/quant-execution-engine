"""🔒 The generated umbrella mirror must match the canonical TOML, byte for byte.

🔴 **This is the check that makes ONE source actually one source.** Without it the mirror is
just a second hand-maintained copy of the same facts — the exact defect the schedule exists
to end, and the way five cost models came to disagree in the first place.

The mirror lives in the UMBRELLA repo, so it is only checkable when this repo is checked out
inside it. That is the normal state; the skip below is for a standalone clone, and it
reports rather than passing quietly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GEN = _REPO / "scripts" / "generate_fee_doc.py"
_DOC = _REPO.parent / "docs" / "reference" / "fee-schedule.md"


@pytest.mark.skipif(
    not _DOC.parent.is_dir(),
    reason="umbrella docs/reference/ absent — standalone clone, mirror not checkable here",
)
def test_the_generated_mirror_matches_the_canonical_schedule() -> None:
    """Runs the generator's own --check, so the test cannot drift from the generator."""
    r = subprocess.run(  # noqa: S603
        [sys.executable, str(_GEN), "--check"],
        capture_output=True,
        text=True,
        cwd=_REPO,
        check=False,
    )
    assert r.returncode == 0, (
        "the umbrella's docs/reference/fee-schedule.md is out of date with the canonical "
        f"TOML.\n{r.stdout}{r.stderr}"
    )


@pytest.mark.skipif(not _DOC.exists(), reason="mirror not present in this checkout")
def test_the_mirror_announces_that_it_is_GENERATED() -> None:
    """A generated file that does not say so invites the hand-edit it cannot survive."""
    text = _DOC.read_text(encoding="utf-8")
    assert "GENERATED, do not edit" in text
    assert "scripts/generate_fee_doc.py" in text, "the mirror must name its regeneration command"


@pytest.mark.skipif(not _DOC.exists(), reason="mirror not present in this checkout")
def test_the_mirror_carries_the_uncorroborated_labels_through() -> None:
    """Provenance must survive generation — it is the reason a reader can trust the numbers.

    Structured provenance (rather than TOML comments) exists precisely so it reaches here.
    """
    text = _DOC.read_text(encoding="utf-8")
    assert "🗣️ operator-supplied" in text and "🏛️ venue-fetched" in text
    assert "**no**" in text, "an uncorroborated figure must render as uncorroborated"
    assert "THB 200 per index point" in text, "the verbatim venue quote must reach the mirror"

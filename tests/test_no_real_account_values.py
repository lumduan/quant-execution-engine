"""🔒 Repo hygiene: no real broker account identifiers in this PUBLIC repository.

**This guard ALLOWLISTS synthetic values rather than blocklisting real ones, and that
choice is the whole point.** A blocklist would have to name the real account numbers and
balances in order to search for them — putting back exactly what it exists to remove, in
a file that is public. It would also only ever catch the spellings whoever wrote it
happened to think of.

That is not hypothetical. The 2026-08-28 redaction pass substituted a balance in its
plain form (``NNNNN.NN``) and missed **the same number written with a thousands
separator** (``NN,NNN.NN``) in seven places — and the *verification* grep used the same
unformatted patterns, so it reported "zero matches" against text that plainly still
contained them. A check that shares its blind spot with the edit it is checking proves
nothing.

⚠️ This paragraph originally illustrated that with the **actual balance**, which put a
real value into the public file whose entire job is to keep real values out — and
because the scan skips its own file, this guard would never have flagged it. The shape
makes the point; the digits were never needed.

Inverting it removes that failure mode by construction: an unrecognised account-shaped
literal fails, whatever its formatting, and adding a real one cannot pass by being spelled
in a way nobody anticipated.

Real values belong in the **private** umbrella at
``docs/reference/liberator-account-reads.md`` and ``docs/reference/streaming-pro-account-reads.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Account-shaped literals: Liberator's 8-digit <investorId><suffix>, and Streaming Pro's
# zero-led 7-digit sub-account.
# The lookarounds exclude UUID segments: a client_order_id like
# "11111111-2222-4333-8444-555555555555" opens with eight digits and is not an account.
# Excluding them in the PATTERN rather than the allowlist matters — an allowlist entry
# would also wave through a real account that happened to be written next to a hyphen.
# Account-shaped literals — one alternative per VENUE GRAMMAR this platform has ever held:
#
#   \d{8}     liberator  <7-digit investorId><suffix>   e.g. 70000002
#   0\d{6}    streaming_pro zero-led sub-account         e.g. 0500007
#   \d{9}     broker-023 / InnovestX                     e.g. 9XXXXXXXX
#   \d{6}-\d  broker-023 hyphenated form                 e.g. 5XXXXX-X
#
# ⚠️ The last two were added 2026-08-28 after two REAL broker-023 accounts were found
# surviving in this PUBLIC repo. Both misses were structural, not careless:
#
#   * the 9-digit one was never matched — the pattern was scoped to exactly the two
#     grammars someone had thought of;
#   * the hyphenated one was matched and then THROWN AWAY by a `(?![-\w])` lookahead
#     added to stop UUID segments matching. Fixing a false POSITIVE created a false
#     NEGATIVE, which on a public repo is strictly the worse of the two.
#
# UUID segments are still excluded, but by SHAPE (a following `-` plus 4 hex plus `-`)
# rather than by any trailing hyphen — so a hyphenated account is no longer waved through.
#
# Enumerating grammars rather than widening to `\d{7,10}` is deliberate: the wide form
# matched 19 round risk caps and wire-format numerics, and every one of those would have
# had to be allowlisted. An allowlist that large stops being a check and becomes a
# dumping ground — the exact failure this file's header argues against.
_ACCOUNT_SHAPED = re.compile(r"(?<![\w-])(?:\d{6}-\d|\d{8,9}|0\d{6})(?![\w])(?!-[0-9a-fA-F]{4}-)")

# Synthetic accounts. They deliberately preserve the venue grammars the tests TEACH:
#   Liberator — 8 digits, suffix 2 = CASH BALANCE (SET), 7 = DERIVATIVE (TFEX)
#   Streaming Pro — SET and TFEX differ by ONE DIGIT, which is why market must be
#   resolved by asking the venue rather than read off the number.
_SYNTHETIC_ACCOUNTS = {
    "70000002",  # liberator SET  / cash        (AWS-shaped)
    "70000007",  # liberator TFEX / derivative  (AWS-shaped)
    "70000012",  # liberator SET  / cash        (HOME-shaped)
    "0500007",  # streaming_pro SET  ┐ one digit apart, on purpose
    "0500009",  # streaming_pro TFEX ┘
}

# Not accounts. Each needs a reason, so the allowlist cannot quietly become a dumping
# ground that defeats the check.
_NOT_ACCOUNTS = {
    "99999999": "obvious sentinel — an account no session holds",
    "00000000": "obvious sentinel — the zero-padded form the venue REFUSES",
    "71937953": "a venue orderNo in a cancel fixture, not an account",
    "16312965": "a venue orderNo in a cancel fixture, not an account",
    "100000000": "a round 100,000,000 risk cap in test_core_risk, not an account",
}

# ⚠️ `.claude` and the repo-root markdown are scanned because omitting them is exactly how
# two REAL broker-023 accounts survived the 2026-08-28 redaction pass on this PUBLIC repo:
# they sat in `.claude/knowledge/decision-log.md` and `docs/plans/ROADMAP.md`, and the
# scan never looked at the first path at all.
_SCANNED = ("src", "tests", "docs", ".claude")
_SCANNED_FILES = ("CLAUDE.md", "README.md", "CHANGELOG.md")
_SKIP_NAMES = {Path(__file__).name}


def _candidates() -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    targets = [p for top in _SCANNED for p in (_ROOT / top).rglob("*")]
    targets += [_ROOT / f for f in _SCANNED_FILES]
    for path in targets:
        if not path.is_file() or path.suffix not in {".py", ".md"}:
            continue
        if path.name in _SKIP_NAMES:
            continue
        for token in _ACCOUNT_SHAPED.findall(path.read_text(encoding="utf-8")):
            # Dates (20260828) and version-ish runs are not account numbers.
            if token.startswith(("19", "20")):
                continue
            found.append((path.relative_to(_ROOT), token))
    return found


def test_no_unrecognised_account_shaped_literals() -> None:
    """Every account-shaped literal must be a declared synthetic value.

    If this fails on a value you just added: do NOT add it to the allowlist unless it is
    genuinely synthetic. A real account number belongs in the private umbrella reference
    docs — this repository is public.
    """
    allowed = _SYNTHETIC_ACCOUNTS | set(_NOT_ACCOUNTS)
    offenders = sorted({(str(p), t) for p, t in _candidates() if t not in allowed})
    assert not offenders, (
        "unrecognised account-shaped literal(s) in a PUBLIC repo — if real, move the "
        "value to the private umbrella's docs/reference/ and substitute a synthetic one:\n"
        + "\n".join(f"  {p}: {t}" for p, t in offenders)
    )


def test_the_guard_can_actually_fail() -> None:
    """Positive control — an allowlist check that matches everything is not a check.

    Without this, widening ``_ACCOUNT_SHAPED`` to something that never matches, or
    letting the allowlist grow to cover any input, would leave the suite green while the
    guard tested nothing.
    """
    # ⚠️ This control originally asserted that two REAL account numbers were absent from
    # the allowlist — which required WRITING THEM HERE, in the public repo this guard
    # exists to keep clean. The guard skips its own file, so it would never have flagged
    # them. Same property, no real values: the pattern must match unknown account-shaped
    # tokens, and the allowlist must be exactly the two declared sets and nothing else.
    allowed = _SYNTHETIC_ACCOUNTS | set(_NOT_ACCOUNTS)
    assert _ACCOUNT_SHAPED.findall("account 12345678 here") == ["12345678"]
    assert _ACCOUNT_SHAPED.findall("sp 0123456 here") == ["0123456"]
    assert "12345678" not in allowed and "0123456" not in allowed
    assert len(allowed) == len(_SYNTHETIC_ACCOUNTS) + len(_NOT_ACCOUNTS)
    # A UUID segment must NOT read as an account.
    assert _ACCOUNT_SHAPED.findall("11111111-2222-4333-8444-555555555555") == []
    # 🔑 Regression control for the two REAL accounts this pattern used to miss
    # (2026-08-28). Synthetic stand-ins, same two grammars — 9 digits, and hyphenated.
    # Without these, a future "simplification" back to `\d{8}|0\d{6}` reads as harmless.
    assert _ACCOUNT_SHAPED.findall("acct 123456789 here") == ["123456789"]
    assert _ACCOUNT_SHAPED.findall("acct 123456-7 here") == ["123456-7"]
    assert "123456789" not in allowed and "123456-7" not in allowed


def test_the_synthetic_values_still_teach_what_the_real_ones_did() -> None:
    """🔑 A scrub that destroys the lesson is a regression, not a fix.

    Two properties are load-bearing in adapter code and its tests, and both survive
    substitution only because the replacements were chosen to preserve them.
    """
    # Streaming Pro: SET and TFEX differ by exactly one digit — the reason the adapter
    # asks the venue which front answers instead of pattern-matching the number.
    sp = sorted(a for a in _SYNTHETIC_ACCOUNTS if a.startswith("0"))
    assert len(sp) == 2
    assert sum(a != b for a, b in zip(sp[0], sp[1], strict=True)) == 1

    # Liberator: 8 digits, and the suffix carries the account type.
    lib = {a for a in _SYNTHETIC_ACCOUNTS if not a.startswith("0")}
    assert all(len(a) == 8 for a in lib)
    assert {a[-1] for a in lib} == {"2", "7"}

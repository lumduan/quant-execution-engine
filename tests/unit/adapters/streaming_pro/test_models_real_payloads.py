"""🔒 `VenueOrderRow` against REAL captured venue payloads — both market shapes.

**This file exists because the model was written against ONE market's field names and silently
defaulted the other's.** SET (`fis`) and TFEX (`seosd`) return *different keys for the same
concepts*, and the model aliased only the TFEX set:

    concept    TFEX (modelled)   SET (NOT modelled)
    matched    matchQty          matched
    balance    balanceQty        balance
    cancelled  cancelQty         cancelled
    status     showStatus        showOrderStatus

With ``extra="ignore"`` plus per-field defaults, every SET row parsed to **zeros** instead of
raising. The one field that *did* raise — ``entryTime``, which SET sends as a bare ``'11:37:02'`` —
is the only reason anyone noticed.

🔴 **The zeros are the dangerous half.** A crash is loud; ``matched = 0`` on a *filled* order is a
silent wrong answer on the single most money-critical field there is. Fixing only ``entryTime``
would have converted the loud failure into the quiet one.

⚠️ The pre-existing tests could not catch this: they were written from an *assumed* shape (see
``test_adapter_cancel_reads.py``, which fed a row carrying ``extOrderNo`` — a key the real venue
does not send). Fixtures built from inference test the inference, not the venue. **The SET fixture
beside this file is a verbatim 41-key capture from a real order** (2026-08-31, order 73709728,
BUY 1 PTT @ 34.25) with only ``accountNo`` swapped for the declared synthetic value, because this
repository is public.
"""

from __future__ import annotations

import json
import re
from datetime import UTC
from decimal import Decimal
from pathlib import Path

from src.quant_execution_engine.adapters.streaming_pro.models import (
    VenueOrderRow,
    parse_order_rows,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _set_payload() -> list[dict[str, object]]:
    data: list[dict[str, object]] = json.loads((_FIXTURES / "venue_order_row_set.json").read_text())
    return data


def test_real_SET_row_parses_at_all() -> None:
    """The captured SET row must not raise.

    Before the fix this died on ``entryTime='11:37:02'`` — a time-only string against a
    ``datetime`` field — which took down ``fetch_venue_orders`` for **every** SP account, and with
    it the reconciler.
    """
    rows = parse_order_rows(_set_payload())
    assert len(rows) == 1


def test_real_SET_row_carries_its_ACTUAL_values_not_defaults() -> None:
    """🔑 The regression that matters: real values, not silent zeros.

    Every assertion here is a value the venue actually sent. Under the old aliases each of these
    resolved to a default — and a default is indistinguishable from a true reading, which is what
    made the bug survivable.
    """
    row = parse_order_rows(_set_payload())[0]

    # the money-critical trio — all silently 0 before the fix
    assert row.balance == 1, "balance must come from SET's `balance`, not default to 0"
    assert row.matched == 0, "matched must come from SET's `matched` (a FILL would read 0 before)"
    assert row.cancelled == 0, "cancelled must come from SET's `cancelled`"

    # status: the SET row spells it out, and we must read the venue's own words
    assert row.status == "O"
    assert row.status_show == "Open(O)", "must come from `showOrderStatus`, not `showStatus`"

    # identity + price, which were already correct — asserted so a refactor cannot quietly lose them
    assert row.order_no == "73709728"
    assert row.symbol == "PTT"
    assert row.side == "Buy"
    assert row.price == Decimal("34.25")
    assert row.price_type == "Limit"
    assert row.validity_type == "Day"


def test_matched_is_not_merely_zero_by_luck() -> None:
    """``matched == 0`` is the true value here, so on its own it cannot prove the alias works.

    A defaulting bug produces 0 for the *wrong* reason. Mutating the captured row to a non-zero
    fill is what separates "read correctly" from "defaulted to the same number" — the guard would
    otherwise pass for a reason unrelated to what it claims to check.
    """
    payload = _set_payload()
    payload[0]["matched"] = 1
    payload[0]["balance"] = 0
    row = parse_order_rows(payload)[0]
    assert row.matched == 1, "a FILLED SET order must not read as unfilled"
    assert row.balance == 0


def test_TFEX_shape_still_parses_after_the_SET_fix() -> None:
    """The fix must ACCEPT BOTH shapes, never swap one for the other.

    Renaming the aliases to SET's names would reproduce this defect exactly, relocated to TFEX —
    so the TFEX field names are pinned here. ``ORDER_MAP.md`` documents this response shape from
    the Gate-#4 live round-trip.
    """
    tfex = [
        {
            "orderNo": "71937953",
            "symbol": "S50Z26",
            "side": "Long",
            "position": "Open",
            "priceType": "Limit",
            "price": 900.5,
            "qty": 2,
            "matchQty": 1,
            "balanceQty": 1,
            "cancelQty": 0,
            "status": "S",
            "showStatus": "Pending(S)",
            "validity": "Day",
        }
    ]
    row = parse_order_rows(tfex)[0]
    assert row.volume == 2
    assert row.matched == 1, "TFEX `matchQty` must still resolve"
    assert row.balance == 1, "TFEX `balanceQty` must still resolve"
    assert row.status_show == "Pending(S)", "TFEX `showStatus` must still resolve"
    assert row.position == "Open"


def test_time_only_and_blank_time_fields_do_not_raise() -> None:
    """SET sends ``entryTime`` time-only and ``cancelTime`` blank-padded.

    ``cancelTime`` arrives as ``'      '`` on a live order and would have hit the identical
    failure the moment an order was actually cancelled — i.e. the next thing we would have tried.
    """
    payload = _set_payload()
    payload[0]["entryTime"] = "11:37:02"
    payload[0]["cancelTime"] = "      "
    row = parse_order_rows(payload)[0]
    # 🔑 The composed instant must be CORRECT, not merely non-raising. entryDate + entryTime are
    # venue-local (Asia/Bangkok); 11:37:02 BKK is 04:37:02 UTC, which is exactly what our own
    # store recorded as created_at for this order. A naive value would raise in the reconciler
    # and a UTC-anchored one would mis-match by seven hours without ever erroring.
    assert row.entry_time is not None
    assert row.entry_time.utcoffset() is not None, "must be tz-aware for the reconciler subtraction"
    assert row.entry_time.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") == "2026-08-31 04:37:02"


def test_unparseable_row_does_not_take_down_the_whole_read() -> None:
    """One malformed row must not blind us to the rest of the book.

    The original failure mode was all-or-nothing: a single bad row raised out of
    ``parse_order_rows`` and the caller saw an exception, not a partial book. For a reconciler
    deciding what is still working at a venue, losing every row because one is odd is the worse
    outcome.
    """
    payload = _set_payload()
    rows = parse_order_rows([*payload, {"orderNo": "X1", "entryTime": "not-a-time"}])
    assert len(rows) >= 1
    assert any(r.order_no == "73709728" for r in rows)


def test_fixture_carries_no_real_account_number() -> None:
    """This repository is PUBLIC; the capture came from a real funded sub-account.

    ⚠️ This assertion originally read ``assert "05xxxxx" not in text`` — naming the real account
    **inside the public file whose job is to keep it out**. `tests/test_no_real_account_values.py`
    caught it, which is exactly what that guard exists for and the second time this specific trap
    has fired. Assert the PROPERTY instead: the only account-shaped value present is the declared
    synthetic one.
    """
    row = _set_payload()[0]
    assert row["accountNo"] == "0500007", "must be the declared synthetic SP SET account"
    text = (_FIXTURES / "venue_order_row_set.json").read_text()
    account_shaped = set(re.findall(r"(?<![\w-])0\d{6}(?![\w])", text))
    assert account_shaped == {"0500007"}, f"unexpected account-shaped values: {account_shaped}"


def test_model_consumes_a_small_subset_of_a_large_payload() -> None:
    """``extra="ignore"`` is deliberate and is NOT what caused the bug.

    The venue sends 41 keys and we model ~14; ``forbid`` would break on every unmodelled field the
    broker adds. The defect was *unverified aliases plus silent defaults*, which the value
    assertions above are the real defence against.
    """
    assert len(_set_payload()[0]) == 41
    assert len(VenueOrderRow.model_fields) < 41


def test_orderNoFis_is_the_SET_cancel_identifier() -> None:
    """🔑 ``extOrderNo`` on the WRITE surface is ``orderNoFis`` on the READ surface.

    This is why it looked undeterminable on 2026-08-31: the read row genuinely has no
    ``extOrderNo`` key, so the field appeared absent rather than renamed. `session:sp-research`
    settled it from the vendor's own de-escaped client bundle at two independent sites — a SET
    cancel sends ``extOrderNo = orderNoFis`` alongside ``orderNo = orderNoSeos``.

    Both values are in the captured row; only the write-side NAME is missing from the read.
    """
    row = parse_order_rows(_set_payload())[0]
    assert row.order_no_fis == "26411", "the SET cancel's extOrderNo, under its read-surface name"
    assert row.order_no == "73709728", "orderNo == orderNoSeos, which is what our store holds"
    assert row.ext_order_no == "", "the venue does NOT send extOrderNo on the read"


def test_the_fis_fallback_is_load_bearing_not_decorative() -> None:
    """Mutation: strip ``orderNoFis`` and the resolution must FAIL, not quietly find something else.

    Without this, a fallback that never fires would look identical to one that works — the same
    silent-default trap that produced the six-alias bug in the first place.
    """
    payload = _set_payload()
    del payload[0]["orderNoFis"]
    row = parse_order_rows(payload)[0]
    assert row.order_no_fis == "", "no orderNoFis -> nothing to fall back to"
    assert (row.ext_order_no or row.order_no_fis or None) is None, (
        "resolution must yield None so cancel() refuses loudly rather than guessing"
    )


def test_TFEX_rows_gain_no_fis_identifier() -> None:
    """FIS-only. seosd/dgw key differently and must not inherit this mapping."""
    row = parse_order_rows([{"orderNo": "71937953", "qty": 1, "matchQty": 0}])[0]
    assert row.order_no_fis == ""

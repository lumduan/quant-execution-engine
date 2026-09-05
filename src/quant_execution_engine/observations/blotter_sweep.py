"""Bank what the venue ACTUALLY CHARGED, from the blotter read, before it disappears.

🔴 **THIS DATA IS PERISHABLE AT ONE DAY AND CANNOT BE BACKFILLED.** `/va/order` is
current-day-scoped and no date parameter exists anywhere in the venue's client
([[TK-0445]] §9.3), and nothing has ever persisted the values ([[TK-0524]] §2). So a
charged fee is visible for one venue day and then **gone forever** — on a node that has run
``STAGE=micro_live`` on real money since 2026-08-25. Every fill before this module existed
is already unrecoverable; that is the whole argument for banking now and refining later.

**It adds no venue call the platform does not already make.** ``GET /orders/{account}`` is
the reconciler's existing poll (12 s). This is the same read, once, on a fixed account list —
**O(1) per account per day, never scaling with universe size**, which is the specific clause
the standing anti-polling rule turns on. It is a separate read rather than a hook into the
reconciler on purpose: a DB write inside the 12 s order-reconciliation loop could delay or
fail order recovery, and cost capture must never be able to do that.

**It needs no PIN** — only ``base_url`` + ``api_key``. A read has no business holding a
trading credential (see [[TK-0529]]).

🔴 **WHAT THIS DELIBERATELY DOES NOT DO: it does not COMPARE.** Every row is banked with
``comparison=None``. The one-sided check needs a basis and a like-for-like observed value,
and three things about ``fee`` are **[UNVERIFIED]** ([[TK-0524]] §2, explicitly "do not
assume any of it"):

  * per-order vs per-fill vs cumulative
  * whether it includes the regulator fee (pre-place separates them; the blotter has one ``fee``)
  * whether it populates only on fill

Dividing an unknown-denominator ``fee`` by ``matched`` to reach a "per contract" figure would
produce a **confident wrong verdict** — a plausible number, stored, that nobody could later
distinguish from a correct one. **One real filled row settles all three**, and the banking
is what makes that row exist. Capture first; compare once the semantics are known.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple, Protocol

import asyncpg

from src.quant_execution_engine.adapters.liberator.mapping import orders_path
from src.quant_execution_engine.adapters.liberator.models import venue_order_rows
from src.quant_execution_engine.db.fee_observations import (
    FeeObservation,
    ObservationKind,
    insert_observation,
)

logger = logging.getLogger(__name__)

SOURCE = "liberator blotter GET /orders/{account}"

# Already-banked lookup. The venue orderNo is the natural key for a CHARGED row and the
# table has no column for it, so it is read out of the verbatim payload. See the module
# docstring in db/fee_observations.py — nothing is lost, because raw_response is complete.
_ALREADY_BANKED = (
    "SELECT raw_response->>'orderNo' AS order_no FROM execution.fee_observations "
    "WHERE observation_kind = 'charged' AND broker = $1 AND account = $2 "
    "AND fee_month = $3 AND raw_response->>'orderNo' = ANY($4::text[])"
)


class _Reader(Protocol):
    """The one transport method this needs. Typed structurally so tests need no HTTP."""

    async def get_json(self, path: str) -> dict[str, Any]: ...


@dataclass
class SweepResult:
    """What one sweep saw. Counts are per-run, not cumulative."""

    accounts_read: int = 0
    rows_seen: int = 0
    filled_rows: int = 0
    banked: int = 0
    already_banked: int = 0
    rows_with_a_fee_value: int = 0
    rows_with_a_NONZERO_fee: int = 0
    malformed_values: int = 0
    """Money fields the venue sent but we could not parse — a wire or parser change.

    NOT the same as an absent field, and deliberately counted so it cannot hide behind a
    NULL that looks like a routine "the venue did not report it".
    """
    errors: list[str] = field(default_factory=list)

    @property
    def semantics_are_now_answerable(self) -> bool:
        """🔑 True once a NON-ZERO fee has been banked against a known ``matched``.

        [[TK-0524]] §2 lists three unknowns about ``fee`` and says one filled order settles
        them. Until this flips, the platform still has **no** charged-cost evidence — and a
        run that banks only zero-fee rows must not be reported as having answered anything.
        """
        return self.rows_with_a_NONZERO_fee > 0


class Money(NamedTuple):
    """A parsed venue money field, with the THREE cases kept apart.

    🔴 The distinction is the entire point of this module's honesty.

    * ``Decimal("28.00")`` — the venue reported a charge
    * ``Decimal("0")``     — the venue reported the charge was **zero**
    * ``None, absent``     — the venue **did not send the field**
    * ``None, malformed``  — the venue sent something we **could not read**

    Collapsing zero into absent would make "we are blind" indistinguishable from "it was
    free", and ``fee: 0`` on an unfilled order is the common case, so the two genuinely
    co-occur in one payload. Collapsing MALFORMED into absent is the subtler version of the
    same error and was in this function until it was noticed: the venue *did* report
    something, and reading it as "not reported" hides a parser or wire change behind a value
    that looks routine. The raw payload is banked verbatim either way, so nothing is lost —
    but the malformed count is what makes anyone go and look.
    """

    value: Decimal | None
    malformed: bool = False


def _money(raw: Any) -> Money:
    """Parse a venue money field into :class:`Money`. See its docstring for the three cases."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return Money(None)
    try:
        return Money(Decimal(str(raw).replace(",", "")))
    except (InvalidOperation, ValueError):
        return Money(None, malformed=True)


def _int_or_zero(raw: Any) -> int:
    """Venue integer, defaulting to 0.

    Zero is the SAFE default here specifically because it is used for ``matched``: an
    unreadable match count reads as *not filled*, so the row is skipped rather than banked
    with a fabricated quantity. Erring toward skipping loses one row of a perishable day;
    erring toward banking would put a made-up quantity in the cost record permanently.
    """
    try:
        return int(str(raw).replace(",", "") or 0)
    except (TypeError, ValueError):
        return 0


async def sweep_account(
    reader: _Reader,
    pool: asyncpg.Pool,
    *,
    account: str,
    broker: str = "liberator",
    now: datetime | None = None,
    result: SweepResult | None = None,
) -> SweepResult:
    """Read one account's blotter and bank every FILLED row not already banked.

    Only rows with ``matched > 0`` are banked: an unfilled order has not been charged, and
    banking its ``fee: 0`` would fill the table with rows that look like evidence of a free
    trade. ``balance``/``cancelled`` are irrelevant here — a partially filled order has been
    charged for the part that filled.

    ⚠️ An unreadable envelope RAISES (via :func:`venue_order_rows`) rather than reading as an
    empty book. That refusal is load-bearing in the reconciler and is inherited deliberately:
    here it would mean silently banking nothing and reporting success.
    """
    res = result if result is not None else SweepResult()
    observed_at = now or datetime.now(UTC)

    rows = venue_order_rows(await reader.get_json(orders_path(account)))
    res.accounts_read += 1
    res.rows_seen += len(rows)

    filled = [r for r in rows if _int_or_zero(r.get("matched")) > 0]
    res.filled_rows += len(filled)
    if not filled:
        return res

    fee_month = FeeObservation(
        observed_at=observed_at,
        kind=ObservationKind.CHARGED,
        source=SOURCE,
        broker=broker,
        account=account,
        symbol="",
        contract_month="",
        side="",
        quantity=1,
        price=Decimal(0),
        raw_response={},
    ).fee_month

    order_nos = [str(r.get("orderNo", "")) for r in filled]
    # Keyed by the SQL alias, not by position: an asyncpg Record supports both, a plain
    # mapping supports only the key, and positional access breaks silently if the SELECT
    # list ever grows. Keying by name makes the fake and the real driver behave identically.
    seen = {
        r["order_no"]
        for r in await pool.fetch(_ALREADY_BANKED, broker, account, fee_month, order_nos)
    }

    for row in filled:
        order_no = str(row.get("orderNo", ""))
        if order_no in seen:
            res.already_banked += 1
            continue
        fee_parsed = _money(row.get("fee"))
        vat_parsed = _money(row.get("vat"))
        fee, vat = fee_parsed.value, vat_parsed.value
        res.malformed_values += fee_parsed.malformed + vat_parsed.malformed
        if fee_parsed.malformed or vat_parsed.malformed:
            logger.warning(
                "unparseable money field on %s/%s — banked with NULL, raw payload kept "
                "verbatim; check for a venue wire change",
                account,
                order_no,
            )
        if fee is not None:
            res.rows_with_a_fee_value += 1
            if fee != 0:
                res.rows_with_a_NONZERO_fee += 1
        matched = _int_or_zero(row.get("matched"))
        try:
            await insert_observation(
                pool,
                FeeObservation(
                    observed_at=observed_at,
                    kind=ObservationKind.CHARGED,
                    source=SOURCE,
                    broker=broker,
                    account=account,
                    symbol=str(row.get("symbol", "")),
                    # The blotter row carries no contract month of its own; the symbol is the
                    # contract (e.g. S50Z26). Left empty rather than parsed out of the symbol,
                    # because a guessed field is worse than an absent one.
                    contract_month="",
                    side=str(row.get("side", "")),
                    quantity=matched,
                    price=_money(row.get("price")).value or Decimal(0),
                    commission=fee,
                    vat=vat,
                    total_fee=None,  # NOT fee+vat: whether `fee` already includes VAT is
                    # [UNVERIFIED] (TK-0524 §2). Summing two fields whose relationship is
                    # unknown would bank a derived number as if it were observed.
                    raw_response=row,
                ),
                comparison=None,  # see the module docstring — semantics are UNVERIFIED
            )
        except Exception as exc:  # one bad row must not lose the rest of a perishable day
            res.errors.append(f"{account}/{order_no}: {exc}")
            logger.exception("failed to bank charged fee for %s/%s", account, order_no)
            continue
        res.banked += 1
    return res


async def sweep_accounts(
    reader: _Reader,
    pool: asyncpg.Pool,
    *,
    accounts: list[str],
    broker: str = "liberator",
    now: datetime | None = None,
) -> SweepResult:
    """Sweep several accounts into one result.

    ⚠️ **One account failing must not abort the others.** The data is perishable, so a
    transport error on account A is not a reason to lose account B's day — the error is
    collected and the sweep continues. A run that collected errors is NOT a clean run, and
    the caller must read ``.errors`` rather than treating a returned result as success.
    """
    res = SweepResult()
    for account in accounts:
        try:
            await sweep_account(reader, pool, account=account, broker=broker, now=now, result=res)
        except Exception as exc:
            res.errors.append(f"{account}: {exc}")
            logger.exception("blotter sweep failed for account %s", account)
    logger.info(
        "blotter fee sweep: accounts=%d rows=%d filled=%d banked=%d already=%d "
        "with_fee=%d nonzero_fee=%d malformed=%d errors=%d",
        res.accounts_read,
        res.rows_seen,
        res.filled_rows,
        res.banked,
        res.already_banked,
        res.rows_with_a_fee_value,
        res.rows_with_a_NONZERO_fee,
        len(res.errors),
    )
    return res

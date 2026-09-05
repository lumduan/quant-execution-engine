"""Persist what the BROKER says a trade costs — ``execution.fee_observations``.

The canonical schedule (:mod:`..reference.fee_schedule`) is a **POLICY**: a fixed,
deliberately conservative basis pinned at the most expensive tier so every strategy
calculation is deterministic and never flatters itself. This module holds the **FACTS** it
is checked against.

🔴 **Deliberately NOT part of** :mod:`.repositories`. That module is imported by
``core.router`` and both adapters, so anything added to it lands in the order path by
construction. A cost corroborator that can fail on a network call has no business there, and
``tests/test_fee_observations.py`` walks the import graph to keep it out.

**Four properties this module enforces structurally, not by convention:**

1. **APPEND-ONLY.** There is no update or delete function, and no ``UPDATE``/``DELETE``
   statement anywhere in the file. The database agrees: ``quant`` holds ``SELECT, INSERT``
   only (quant-infra-db PR #31). An observation that can be edited is not evidence.
2. **``observation_kind`` is REQUIRED on every read.** An ``indicative_quote`` (what a
   hypothetical order *would* cost) and a ``charged`` amount (what a real fill *did* cost)
   have different epistemic status and must never be averaged. A default would let a quote
   silently stand in for evidence, so there is no default.
3. **``fee_month`` is DERIVED, never passed.** Two independent inputs that must agree will
   eventually disagree; one input cannot.
4. **The verdict cannot be omitted.** :func:`record_observation` always fills
   ``basis_value``/``basis_effective_from``/``verdict`` from the comparison, so no row can
   exist without the check that gives it meaning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from zoneinfo import ZoneInfo

import asyncpg
from pydantic import BaseModel, ConfigDict

from src.quant_execution_engine.db.errors import RepositoryError
from src.quant_execution_engine.reference.fee_schedule import Comparison, FeeSchedule, Verdict

logger = logging.getLogger(__name__)

# 🔴 Commission tiers accrue on the VENUE's calendar month, which is Bangkok, not UTC. An
# observation at 2026-09-01 03:00 BKK is 2026-08-31 20:00 UTC: anchoring the month in UTC
# would file it under the PREVIOUS month, against the wrong tier, and nothing would raise.
# Same reasoning and same constant as adapters/liberator/reconciler.py's venue day.
_VENUE_TZ = ZoneInfo("Asia/Bangkok")


class ObservationKind(StrEnum):
    """What kind of statement the row records. NEVER averaged across."""

    INDICATIVE_QUOTE = "indicative_quote"
    """What a hypothetical order WOULD cost (a pre-place quote). Not evidence of a charge."""

    CHARGED = "charged"
    """What a real fill DID cost (a blotter read). The stronger claim."""


def venue_date(ts: datetime) -> date:
    """The **Bangkok** calendar date of ``ts``.

    A naive timestamp is read as UTC — the platform's storage convention — rather than as
    local time, which is what ``astimezone`` would otherwise assume and would silently shift
    the boundary on any machine not running in UTC.
    """
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return aware.astimezone(_VENUE_TZ).date()


def venue_fee_month(ts: datetime) -> date:
    """The first day of ``ts``'s Bangkok calendar month — the tier-accrual period."""
    return venue_date(ts).replace(day=1)


def next_fee_month(fee_month: date) -> date:
    """The first day of the month after ``fee_month`` (the exclusive upper bound)."""
    return (fee_month.replace(day=28) + timedelta(days=4)).replace(day=1)


@dataclass(frozen=True)
class FeeObservation:
    """One observation, before it has been checked against the basis.

    ``fee_month`` is deliberately absent — it is derived from ``observed_at`` (property 3).
    """

    observed_at: datetime
    kind: ObservationKind
    source: str
    broker: str
    account: str
    symbol: str
    contract_month: str
    side: str
    quantity: int
    price: Decimal
    raw_response: dict[str, Any]
    is_roll_boundary: bool = False
    commission: Decimal | None = None
    exchange_fee: Decimal | None = None
    clearing_fee: Decimal | None = None
    vat: Decimal | None = None
    total_fee: Decimal | None = None
    month_to_date_contracts: int | None = None

    @property
    def fee_month(self) -> date:
        """The venue month this observation is filed under. Derived, never supplied."""
        return venue_fee_month(self.observed_at)


class FeeObservationRow(BaseModel):
    """One ``execution.fee_observations`` row, as read back."""

    model_config = ConfigDict(frozen=True)

    id: int
    observed_at: datetime
    observation_kind: ObservationKind
    source: str
    broker: str
    account: str
    symbol: str
    contract_month: str
    is_roll_boundary: bool
    side: str
    quantity: int
    price: Decimal
    commission: Decimal | None
    exchange_fee: Decimal | None
    clearing_fee: Decimal | None
    vat: Decimal | None
    total_fee: Decimal | None
    fee_month: date
    month_to_date_contracts: int | None
    raw_response: dict[str, Any]
    basis_value: Decimal | None
    basis_effective_from: date | None
    verdict: Verdict | None
    inserted_at: datetime

    @classmethod
    def from_record(cls, record: Any) -> Self:
        """Build from an asyncpg ``Record``.

        ⚠️ asyncpg returns ``JSONB`` as a **string** unless a codec is registered, so
        ``raw_response`` is decoded here. Without this the field would arrive as a ``str``
        that *looks* like data and fails only at the first key access.
        """
        values = {name: record[name] for name in cls.model_fields}
        raw = values.get("raw_response")
        if isinstance(raw, str):
            values["raw_response"] = json.loads(raw)
        return cls(**values)


@dataclass(frozen=True)
class Recorded:
    """What :func:`record_observation` did: the row id and the check that justified it."""

    observation_id: int
    comparison: Comparison

    @property
    def should_alert(self) -> bool:
        """🔴 ONE-SIDED — only an observation ABOVE the basis alerts. See ``Comparison``."""
        return self.comparison.should_alert


_INSERT = (
    "INSERT INTO execution.fee_observations "
    "(observed_at, observation_kind, source, broker, account, symbol, contract_month, "
    "is_roll_boundary, side, quantity, price, commission, exchange_fee, clearing_fee, "
    "vat, total_fee, fee_month, month_to_date_contracts, raw_response, basis_value, "
    "basis_effective_from, verdict) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, "
    "$18, $19::jsonb, $20, $21, $22) "
    "RETURNING id"
)

_SELECT_BY_MONTH = (
    "SELECT * FROM execution.fee_observations "
    "WHERE observation_kind = $1 AND fee_month = $2 AND symbol = $3 "
    "ORDER BY observed_at"
)

_SELECT_LATEST = (
    "SELECT * FROM execution.fee_observations "
    "WHERE observation_kind = $1 AND symbol = $2 "
    "ORDER BY observed_at DESC LIMIT 1"
)

# Tier context from REAL fills, not from probes — see month_to_date_contracts().
_SUM_FILLED_QTY = (
    "SELECT COALESCE(SUM(f.quantity), 0)::bigint "
    "FROM execution.fills f JOIN execution.orders o USING (client_order_id) "
    "WHERE o.broker = $1 AND o.account = $2 AND o.market = $3 "
    # exec_ts is TIMESTAMPTZ, so ONE `AT TIME ZONE` yields the Bangkok wall-clock value.
    # ⚠️ The chained form `AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Bangkok'` is WRONG here and
    # was in this query until it was tested: on a timestamptz the first conversion produces a
    # NAIVE UTC timestamp and the second then reads that naive value AS Bangkok time, landing
    # 7 hours early. Measured: 2026-08-31 20:00Z is 2026-09-01 in Bangkok, and the chained
    # form returned 2026-08-31 — off by one day exactly AT THE MONTH BOUNDARY, which is the
    # only place a monthly-tier query can be wrong, and it raises nothing.
    "AND (f.exec_ts AT TIME ZONE 'Asia/Bangkok')::date >= $4 "
    "AND (f.exec_ts AT TIME ZONE 'Asia/Bangkok')::date < $5"
)


async def insert_observation(
    pool: asyncpg.Pool,
    observation: FeeObservation,
    *,
    comparison: Comparison | None = None,
) -> int:
    """Append one row and return its id. **There is no update path — by design.**

    Prefer :func:`record_observation`, which cannot produce an unchecked row. This exists
    for the case where the schedule genuinely has no basis to compare against (a recorded
    GAP), where ``comparison=None`` is the honest value rather than a fabricated verdict.
    """
    try:
        row_id: int = await pool.fetchval(
            _INSERT,
            observation.observed_at,
            observation.kind.value,
            observation.source,
            observation.broker,
            observation.account,
            observation.symbol,
            observation.contract_month,
            observation.is_roll_boundary,
            observation.side,
            observation.quantity,
            observation.price,
            observation.commission,
            observation.exchange_fee,
            observation.clearing_fee,
            observation.vat,
            observation.total_fee,
            observation.fee_month,
            observation.month_to_date_contracts,
            json.dumps(observation.raw_response),
            comparison.basis if comparison else None,
            comparison.basis_effective_from if comparison else None,
            comparison.verdict.value if comparison else None,
        )
    except asyncpg.exceptions.CheckViolationError as exc:
        # A column CHECK the row does not satisfy — an unknown observation_kind, a
        # non-positive quantity, a negative price. Mapped rather than allowed to escape as a
        # bare 500, which reads as RETRYABLE and would be retried forever (TK-0395).
        raise RepositoryError(f"fee observation violates a column CHECK: {exc}") from exc
    except asyncpg.exceptions.InsufficientPrivilegeError as exc:
        # 🔴 Named explicitly because this failure LIES. The grant is SELECT, INSERT only
        # (PR #31); if it were ever narrowed, `quant` would stop seeing the table in
        # information_schema entirely and the symptom would read as "the table is missing".
        raise RepositoryError(
            "no INSERT privilege on execution.fee_observations — the table is present but "
            f"unwritable; check the GRANT, not the migration: {exc}"
        ) from exc
    return row_id


async def record_observation(
    pool: asyncpg.Pool,
    schedule: FeeSchedule,
    observation: FeeObservation,
    *,
    instrument: str,
    field: str,
    observed_value: Decimal,
) -> Recorded:
    """Check one observation against the basis in force, then append it with its verdict.

    🔑 **The comparison and the write are one operation on purpose.** Splitting them would
    permit a row with no verdict, or a verdict computed against a different day's basis than
    the one the row claims — and both look identical to a correct row afterwards.

    The basis is resolved **as of the observation's own venue day**, not "today": an
    observation banked last month must be checked against the basis that was in force then,
    which is what makes an old alert reconstructible.

    Returns :class:`Recorded`; ``.should_alert`` is True only when the observation came in
    **ABOVE** the basis — the one direction that means strategy costs are understated.
    """
    comparison = schedule.compare(
        instrument, field, observed_value, on=venue_date(observation.observed_at)
    )
    row_id = await insert_observation(pool, observation, comparison=comparison)
    if comparison.should_alert:
        logger.warning(
            "fee observation ABOVE basis: %s.%s observed=%s basis=%s (effective %s) — the "
            "conservative basis is no longer conservative; every strategy calculation using "
            "it is UNDERSTATING cost",
            instrument,
            field,
            comparison.observed,
            comparison.basis,
            comparison.basis_effective_from,
        )
    return Recorded(observation_id=row_id, comparison=comparison)


async def fetch_observations(
    pool: asyncpg.Pool,
    *,
    kind: ObservationKind,
    fee_month: date,
    symbol: str,
) -> list[FeeObservationRow]:
    """Rows for one venue month and symbol, oldest first.

    ``kind`` has **no default** (property 2): a quote and a charge are different claims, and
    a caller who did not choose between them almost certainly did not mean to mix them.
    """
    records = await pool.fetch(_SELECT_BY_MONTH, kind.value, fee_month, symbol)
    return [FeeObservationRow.from_record(r) for r in records]


async def fetch_latest(
    pool: asyncpg.Pool, *, kind: ObservationKind, symbol: str
) -> FeeObservationRow | None:
    """The most recent observation of one kind for one symbol, or ``None`` if there is none.

    ``None`` means *nothing has ever been observed* — it is not a zero-cost reading, and no
    caller may treat it as one.
    """
    record = await pool.fetchrow(_SELECT_LATEST, kind.value, symbol)
    return FeeObservationRow.from_record(record) if record is not None else None


async def month_to_date_contracts(
    pool: asyncpg.Pool, *, broker: str, account: str, market: str, fee_month: date
) -> int:
    """Filled quantity for one account in one **Bangkok** month — the tier context.

    ⚠️ **A LOWER BOUND, not the account's true monthly volume.** It counts only fills THIS
    ENGINE recorded. Anything traded on the same broker account outside this engine is
    invisible here, so the real tier may be better than this number implies. Stored beside
    each observation so a declining rate can be read as *a tier accruing* rather than as the
    schedule drifting — but a decline it cannot explain is a reason to check the account
    statement, not to conclude drift.

    ⚠️ **Units follow the market.** For TFEX this is contracts; for SET it is shares, and the
    name would then be wrong. Pass ``market`` deliberately.
    """
    total: int = await pool.fetchval(
        _SUM_FILLED_QTY, broker, account, market, fee_month, next_fee_month(fee_month)
    )
    return total

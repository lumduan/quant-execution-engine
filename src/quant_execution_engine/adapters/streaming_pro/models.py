"""Streaming-Pro bridge wire models — the place result + the order-row subset we consume.

Shapes match the bridge's ``app/services/order_service`` (live-captured at the bridge's Gate #4):
a place answers the flat ``OrderResult`` ``{ok, order_no, ext_order_no, status, reject_reason}``
(a 4xx instead carries FastAPI's ``{detail}``); ``GET /orders`` returns the raw broker rows. Models
are tolerant (``extra="ignore"``) — the engine consumes a subset and must not break on extra fields.
The exact ``/orders`` list-row shape is validated in a ``micro_live`` soak (documented).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class BridgePlace(BaseModel):
    """One bridge place/cancel response. ``ok`` is the single success predicate."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    ok: bool = False
    order_no: str | None = None
    ext_order_no: str | None = None
    status: str | None = None
    reason: str | None = Field(default=None, alias="reject_reason")
    # FastAPI error responses (422 cap / 403 public / 401 bad-key / 501) carry ``detail``.
    detail: Any = None

    def reject_reason(self) -> str:
        """A non-empty reason — a reject is never swallowed."""
        for part in (self.reason, self.detail, self.status):
            if part:
                return str(part)
        return "streaming_pro rejected the request"


_VENUE_TZ = ZoneInfo("Asia/Bangkok")
"""Streaming Pro reports order times in venue-local wall-clock, with no offset on the wire."""


class VenueOrderRow(BaseModel):
    """The order-row subset the reconciler/reads consume (``matchQty`` is CUMULATIVE)."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    order_no: str = Field(alias="orderNo")
    account_no: str = Field(default="", alias="accountNo")
    # ⚠️ SET does not send this AT ALL (verified against a real capture 2026-08-31). It is required
    # to build a SET cancel, so the adapter must FAIL LOUD rather than treat "" as usable.
    ext_order_no: str = Field(default="", alias="extOrderNo")
    symbol: str = ""
    side: str = ""  # "Buy"|"Sell" (equity) | "Long"|"Short" (deriv)
    position: str | None = None  # "Open"|"Close" (TFEX) | None (SET)
    price_type: str = Field(default="", alias="priceType")
    price: Decimal | None = None
    status: str = ""
    reject_reason: str = Field(default="", alias="rejectReason")
    validity_type: str = Field(default="", alias="validity")

    # 🔴 THE TWO MARKETS SEND DIFFERENT KEYS FOR THE SAME CONCEPTS. Modelling only one of them is
    # what broke this: every SET row parsed to ZEROS instead of raising, because each field had a
    # default and `extra="ignore"` swallowed the real keys. A crash is loud; `matched = 0` on a
    # FILLED order is a silent wrong answer on the most money-critical field there is.
    #
    #   concept    TFEX (seosd)   SET (fis)
    #   volume     qty            vol
    #   matched    matchQty       matched
    #   balance    balanceQty     balance
    #   cancelled  cancelQty      cancelled
    #   status     showStatus     showOrderStatus
    #
    # Accept BOTH. Do NOT "tidy" this to one set — dropping either side reproduces the defect in
    # the other market, which is the same bug wearing a different hat.
    volume: int = Field(default=0, validation_alias=AliasChoices("qty", "vol"))
    matched: int = Field(default=0, validation_alias=AliasChoices("matchQty", "matched"))
    balance: int = Field(default=0, validation_alias=AliasChoices("balanceQty", "balance"))
    cancelled: int = Field(default=0, validation_alias=AliasChoices("cancelQty", "cancelled"))
    status_show: str = Field(
        default="", validation_alias=AliasChoices("showStatus", "showOrderStatus")
    )

    entry_time: datetime | None = Field(default=None, alias="entryTime")

    @model_validator(mode="before")
    @classmethod
    def _compose_entry_time(cls, data: Any) -> Any:
        """Normalise ``entryTime`` to a tz-aware datetime or ``None`` — never leave a raw string.

        SET splits the instant across ``entryDate`` + a bare ``entryTime`` (``'11:37:02'``). A
        time-only string against a ``datetime`` field raises, and that ONE ValidationError took down
        ``fetch_venue_orders`` for EVERY Streaming-Pro account — and with it the reconciler, the
        open-orders read and the SET cancel path, simultaneously.

        🔑 The date is NOT invented: the venue sends ``entryDate`` in the same row, so the
        instant is COMPOSED from data it actually gave us. Times are venue-local
        (Asia/Bangkok) and returned tz-aware, because the reconciler subtracts this from a
        UTC ``created_at`` — a naive value would raise there, and a wrongly-anchored one
        would mis-match by seven hours in silence.

        Anything else degrades to ``None``, which ``reconciler.py`` already handles by skipping the
        fuzzy match. **One odd row must never blind us to the rest of the book:** raising here
        discards every other order the venue reported, which for a reconciler deciding what is still
        live at a venue is the worse failure.
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("entryTime")
        if raw is None or isinstance(raw, datetime):
            return data
        if not isinstance(raw, str) or not raw.strip():
            return {**data, "entryTime": None}
        text = raw.strip()
        if len(text) <= 8 and ":" in text:  # time-only: needs the date the venue sent alongside
            date_part = str(data.get("entryDate") or "").strip()
            try:
                composed = datetime.strptime(f"{date_part} {text}", "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return {**data, "entryTime": None}
            return {**data, "entryTime": composed.replace(tzinfo=_VENUE_TZ)}
        try:
            return {**data, "entryTime": datetime.fromisoformat(text)}
        except ValueError:
            return {**data, "entryTime": None}


_LIST_KEYS = ("orders", "list", "results", "data", "portfolioList")


def parse_order_rows(payload: Any) -> list[VenueOrderRow]:
    """Extract order rows from a ``GET /orders`` response (tolerant of list-or-wrapped).

    The bridge returns the raw broker JSON — either a bare list of rows or a dict wrapping one
    under a common key. Any non-row / missing level yields ``[]`` (an empty book is not an error;
    transport failures raise before reaching here).
    """
    raw: Any = payload
    if isinstance(payload, dict):
        raw = next((payload[k] for k in _LIST_KEYS if isinstance(payload.get(k), list)), [])
    if not isinstance(raw, list):
        return []
    return [VenueOrderRow.model_validate(row) for row in raw if isinstance(row, dict)]

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

from pydantic import BaseModel, ConfigDict, Field


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


class VenueOrderRow(BaseModel):
    """The order-row subset the reconciler/reads consume (``matchQty`` is CUMULATIVE)."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    order_no: str = Field(alias="orderNo")
    account_no: str = Field(default="", alias="accountNo")
    ext_order_no: str = Field(default="", alias="extOrderNo")
    symbol: str = ""
    side: str = ""  # "Buy"|"Sell" (equity) | "Long"|"Short" (deriv)
    position: str | None = None  # "Open"|"Close" (TFEX) | None (SET)
    price_type: str = Field(default="", alias="priceType")
    volume: int = Field(default=0, alias="qty")
    price: Decimal | None = None
    matched: int = Field(default=0, alias="matchQty")
    balance: int = Field(default=0, alias="balanceQty")
    cancelled: int = Field(default=0, alias="cancelQty")
    status: str = ""
    status_show: str = Field(default="", alias="showStatus")
    reject_reason: str = Field(default="", alias="rejectReason")
    validity_type: str = Field(default="", alias="validity")
    entry_time: datetime | None = Field(default=None, alias="entryTime")


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

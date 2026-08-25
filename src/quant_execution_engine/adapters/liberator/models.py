"""Liberator wire models — the upstream envelope and the OrderItem subset we consume.

Shapes verified against ``broker-api/liberator-trading-api`` (umbrella submodule):
every endpoint answers ``{success, message, data: {errorCode, errMsg, result}}``;
``errorCode == 0`` with no ``errMsg`` is success; a successful place carries the
venue order number at ``data.result.orderNo``.

⚠️ **The orders query is NOT consistently under ``data``.** The bridge populates
``data`` on some routes and ``raw_response`` on others (GH #208 Defect 2, still open),
so :func:`parse_order_items` accepts either — and RAISES rather than reporting an
unreadable envelope as an empty order book. Models are tolerant (``extra="ignore"``)
about *fields*; the envelope is deliberately not tolerant about *shape*.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.quant_execution_engine.adapters.liberator.errors import LiberatorTransportError


class LiberatorData(BaseModel):
    """The inner ``data`` envelope: errorCode / errMsg / result."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    error_code: int | None = Field(default=None, alias="errorCode")
    err_msg: str | None = Field(default=None, alias="errMsg")
    result: dict[str, Any] | list[Any] | None = None


class LiberatorEnvelope(BaseModel):
    """One upstream response. ``ok`` is the single success predicate."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    success: bool = False
    message: str | None = None
    data: LiberatorData | None = None
    # FastAPI error responses (e.g. 401 invalid api-key) carry only ``detail``.
    detail: Any = None

    @property
    def ok(self) -> bool:
        """errorCode == 0 and no errMsg (the upstream success contract)."""
        return (
            self.success
            and self.data is not None
            and self.data.error_code == 0
            and not self.data.err_msg
        )

    def reject_reason(self) -> str:
        """A non-empty venue-truth reason — a reject is never swallowed."""
        parts: list[str] = []
        if self.data is not None:
            if self.data.error_code not in (None, 0):
                parts.append(f"errorCode={self.data.error_code}")
            if self.data.err_msg:
                parts.append(self.data.err_msg)
        if not parts and self.message:
            parts.append(self.message)
        if not parts and self.detail is not None:
            parts.append(str(self.detail))
        return "; ".join(parts) or "liberator rejected the request"

    def order_no(self) -> str | None:
        """The venue order number from a successful place (``data.result.orderNo``)."""
        if self.data is None or not isinstance(self.data.result, dict):
            return None
        value = self.data.result.get("orderNo")
        return str(value) if value is not None else None


class VenueOrderItem(BaseModel):
    """The ``OrderItem`` subset the reconciler consumes (read side).

    NOTE: the read-side ``side`` is ``B``/``S`` — different from the write-side
    ``Buy``/``Sell``/``Long``/``Short`` strings. ``matched`` is CUMULATIVE.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    order_no: str = Field(alias="orderNo")
    account_no: str = Field(default="", alias="accountNo")
    symbol: str = ""
    side: str = ""  # "B" | "S"
    position: str | None = None
    price_type: str = Field(default="", alias="priceType")
    iceberg_vol: int = Field(default=0, alias="icebergVol")
    volume: int = 0
    price: Decimal | None = None
    matched: int = 0
    balance: int = 0
    cancelled: int = 0
    amount: Decimal | None = None
    status: str = ""
    status_show: str = Field(default="", alias="statusShow")
    reject_code: str = Field(default="", alias="rejectCode")
    can_cancel: bool = Field(default=False, alias="canCancel")
    validity_type: str = Field(default="", alias="validityType")
    stop_price: Decimal | None = Field(default=None, alias="stopPrice")
    stop_symbol: str | None = Field(default=None, alias="stopSymbol")
    entry_time: datetime | None = Field(default=None, alias="entryTime")
    trade_time: datetime | None = Field(default=None, alias="tradeTime")


def parse_order_items(payload: dict[str, Any]) -> list[VenueOrderItem]:
    """Extract the venue order rows from an orders-query response.

    Accepts **either** envelope key — ``raw_response.result.list`` or
    ``data.result.list`` — because the bridge is inconsistent between them and the
    decision on which to standardise is still open (GH #208 Defect 2). Accepting both
    is what lets that bridge change land in either order without a flag day.

    🔴 **AN UNPARSEABLE ENVELOPE RAISES; IT DOES NOT RETURN ``[]``.**

    This function used to be "tolerant: any missing level yields ``[]``", which made an
    envelope it could not read **indistinguishable from a venue with no open orders** —
    and the two drive opposite actions. On an empty book the reconciler resolves a stuck
    ``PENDING_NEW`` to ``REJECTED ("ack_lost_unmatched")`` at 60 s and confirms a
    ``PENDING_CANCEL`` as ``CANCELLED``. So a shape change it could not parse would have
    **marked live orders terminal**, silently, one poll at a time.

    That was not hypothetical: this function reads ``data`` while
    ``adapter._venue_result`` reads ``raw_response`` for the balance routes, so
    standardising the bridge on ``raw_response`` — the obvious tidy-up — would have done
    exactly that.

    :class:`LiberatorTransportError` is reused deliberately rather than inventing an
    error type: the plumbing for it is already the fail-safe one. ``reconcile_once``
    catches it and **skips the account** untouched, and ``resolve_order_now`` lets it
    propagate so the submit path reports ``UNKNOWN`` — "we could not read the venue",
    which is exactly what has happened.

    An envelope that *does* carry a ``result`` object with no ``list`` is a genuinely
    empty book and correctly returns ``[]``.
    """
    result: Any = None
    for envelope_key in ("raw_response", "data"):
        envelope = payload.get(envelope_key)
        if isinstance(envelope, dict) and isinstance(envelope.get("result"), dict):
            result = envelope["result"]
            break
    if result is None:
        raise LiberatorTransportError(
            "liberator orders: no result object under 'raw_response' or 'data' — "
            "refusing to report this as an empty order book, because an empty book "
            "resolves live orders to REJECTED/CANCELLED (GH #208, [[TK-0428]])"
        )
    raw_items = result.get("list")
    if not isinstance(raw_items, list):
        return []  # result present, no list => the venue really has no open orders
    return [VenueOrderItem.model_validate(item) for item in raw_items if isinstance(item, dict)]

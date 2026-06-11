"""Liberator wire models — the upstream envelope and the OrderItem subset we consume.

Shapes verified against ``third_party/liberator-trading-api`` (pinned submodule):
every endpoint answers ``{success, message, data: {errorCode, errMsg, result}}``;
``errorCode == 0`` with no ``errMsg`` is success; a successful place carries the
venue order number at ``data.result.orderNo``; the orders query nests its items
at ``data.result.list``. Models are tolerant (``extra="ignore"``) — the engine
consumes a subset and must not break when upstream adds fields.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    """Extract ``data.result.list`` items from an orders-query response.

    Tolerant: any missing level yields ``[]`` (an empty book is not an error;
    transport-level failures raise before reaching here).
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    raw_items = result.get("list")
    if not isinstance(raw_items, list):
        return []
    return [VenueOrderItem.model_validate(item) for item in raw_items if isinstance(item, dict)]

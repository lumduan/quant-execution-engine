"""Settrade wire models — tolerant Pydantic boundaries for both order books.

One ``SettradeOrderItem`` covers SET equity and TFEX derivatives: the two books
spell the same concepts differently (``qty``/``vol``, ``matchQty``/``matched``,
``orderNo`` int vs str, ``showStatus``/``showOrderStatus``), so the fields use
``AliasChoices`` and tolerate either. Models are tolerant (``extra="ignore"``,
``populate_by_name=True``) — the engine consumes a subset and must not break when
upstream adds fields. Wire floats become ``Decimal`` via a ``str()`` round-trip
(hard rule 4: money is never ``float`` in our models).

Field shapes pinned from the official venue docs + the ``settrade-v2`` 2.2.1 SDK
(see ``/tmp/settrade_docs/PINNED.md``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

logger = logging.getLogger(__name__)

_TOLERANT = ConfigDict(populate_by_name=True, extra="ignore")


def _to_decimal(value: Any) -> Decimal | None:
    """Parse a wire number into ``Decimal`` via a ``str()`` round-trip.

    ``float`` is round-tripped through ``str()`` so ``1299.0`` becomes the exact
    ``Decimal("1299.0")`` rather than the binary-float artefact ``Decimal`` would
    otherwise produce. ``None``/blank yields ``None``; unparseable yields ``None``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class SettradeTokenResponse(BaseModel):
    """The OAuth login/refresh response (``/api/oam/v1/.../login``)."""

    model_config = _TOLERANT

    token_type: str
    access_token: str
    refresh_token: str
    expires_in: int


class SettradeErrorBody(BaseModel):
    """A structured non-2xx body: ``{code, message}`` (code coerced to ``str``)."""

    model_config = _TOLERANT

    code: str
    message: str

    @field_validator("code", mode="before")
    @classmethod
    def _coerce_code(cls, value: Any) -> Any:
        return str(value) if value is not None else value


class SettradePlaceResponse(BaseModel):
    """A successful place response — we only need the venue order number.

    Errors raise before this is built (the client maps a structured non-2xx into
    :class:`SettradeVenueRejection`), so no ``ok`` predicate is needed here.
    """

    model_config = _TOLERANT

    order_no: str = Field(validation_alias=AliasChoices("orderNo", "orderNumber"))

    @field_validator("order_no", mode="before")
    @classmethod
    def _order_no_to_str(cls, value: Any) -> Any:
        return str(value) if value is not None else value


class SettradeOrderItem(BaseModel):
    """One order row — covers both books (place response / get_order / get_orders).

    The reconciler consumes this read side. ``matched`` is CUMULATIVE (E18 fill
    deltas). ``rejected`` distinguishes a real reject from the neutral ``0``/empty
    reject code the venue stamps on every healthy order.
    """

    model_config = _TOLERANT

    order_no: str = Field(validation_alias="orderNo")
    account_no: str = Field(default="", validation_alias="accountNo")
    symbol: str = ""
    side: str = ""  # "Buy"/"Sell" (equity) | "Long"/"Short" (deriv)
    position: str | None = None  # equity has none
    price_type: str = Field(default="", validation_alias="priceType")
    price: Decimal | None = None
    quantity: int = Field(default=0, validation_alias=AliasChoices("qty", "vol"))
    matched: int = Field(default=0, validation_alias=AliasChoices("matchQty", "matched"))
    balance: int = Field(default=0, validation_alias=AliasChoices("balanceQty", "balance"))
    cancelled: int = Field(default=0, validation_alias=AliasChoices("cancelQty", "cancelled"))
    iceberg_vol: int = Field(default=0, validation_alias="icebergVol")
    status: str = ""
    show_status: str = Field(
        default="", validation_alias=AliasChoices("showStatus", "showOrderStatus")
    )
    status_meaning: str = Field(
        default="",
        validation_alias=AliasChoices("statusMeaning", "showOrderStatusMeaning"),
    )
    reject_code: str | None = Field(default=None, validation_alias="rejectCode")
    reject_reason: str | None = Field(default=None, validation_alias="rejectReason")
    can_cancel: bool = Field(default=False, validation_alias="canCancel")
    validity: str = ""
    entry_time: datetime | None = Field(default=None, validation_alias="entryTime")
    trade_date: str | None = Field(default=None, validation_alias="tradeDate")

    @field_validator("order_no", mode="before")
    @classmethod
    def _order_no_to_str(cls, value: Any) -> Any:
        # Derivatives ``orderNo`` is an int; equity is a str — normalize to str.
        return str(value) if value is not None else value

    @field_validator("price", mode="before")
    @classmethod
    def _parse_price(cls, value: Any) -> Any:
        return _to_decimal(value)

    @field_validator("reject_code", mode="before")
    @classmethod
    def _reject_code_to_str(cls, value: Any) -> Any:
        return str(value) if value is not None else value

    @property
    def rejected(self) -> bool:
        """True iff the venue stamped a real reject code or reason.

        ``0``/``""``/``None`` reject codes are the neutral "no reject" markers the
        venue puts on every healthy order; only a non-neutral code (or a non-empty
        ``rejectReason``) means the order was actually rejected.
        """
        code = self.reject_code
        if code is not None and code not in ("", "0"):
            return True
        return bool(self.reject_reason)


def parse_order_items(payload: object) -> list[SettradeOrderItem]:
    """Extract order rows from a get_orders response (a bare JSON list).

    The venue returns a bare list; we tolerate ``{"data": [...]}`` / ``{"orders":
    [...]}`` wrappers and anything else yields ``[]`` (an empty book is not an
    error). Individually unparseable rows are skipped with a redacted WARNING.
    """
    rows: object = payload
    if isinstance(payload, dict):
        rows = payload.get("data", payload.get("orders"))
    if not isinstance(rows, list):
        return []
    items: list[SettradeOrderItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            items.append(SettradeOrderItem.model_validate(row))
        except ValueError:
            logger.warning("settrade order row skipped (unparseable) keys=%s", sorted(row))
    return items


class SettradeAccountInfo(BaseModel):
    """Account-info subset for both books (back ``get_account`` later).

    Buying-power-like fields differ per book; we keep them tolerant. From the
    venue docs: equity exposes ``lineAvailable`` (credit-balance available cash
    line) and ``excessEquity``; derivatives exposes ``excessEquity`` and
    ``cashBalance``. ``cash_balance`` aliases the equity ``availableCashBalance``
    or the derivatives ``cashBalance`` — whichever the response carries.
    """

    model_config = _TOLERANT

    line_available: Decimal | None = Field(default=None, validation_alias="lineAvailable")
    excess_equity: Decimal | None = Field(default=None, validation_alias="excessEquity")
    cash_balance: Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("availableCashBalance", "cashBalance"),
    )
    equity: Decimal | None = Field(
        default=None, validation_alias=AliasChoices("equityBalance", "equity")
    )

    @field_validator("line_available", "excess_equity", "cash_balance", "equity", mode="before")
    @classmethod
    def _parse_money(cls, value: Any) -> Any:
        return _to_decimal(value)


class SettradePortfolioItem(BaseModel):
    """One portfolio row — symbol + best-effort signed/net quantity.

    Equity rows carry ``currentVolume`` (always long); derivatives rows carry
    ``actualLongPosition``/``actualShortPosition``. ``net_quantity`` is the signed
    net derived in the validator: equity ``currentVolume``, or deriv long - short.
    """

    model_config = _TOLERANT

    symbol: str = ""
    current_volume: int | None = Field(default=None, validation_alias="currentVolume")
    long_position: int = Field(default=0, validation_alias="actualLongPosition")
    short_position: int = Field(default=0, validation_alias="actualShortPosition")

    @property
    def net_quantity(self) -> int:
        """Signed net: equity ``currentVolume`` (long), else deriv long - short."""
        if self.current_volume is not None:
            return self.current_volume
        return self.long_position - self.short_position


class SettradeTradeItem(BaseModel):
    """One executed trade row (``GET /trades``).

    Reserved for Phase 5 per-fill enrichment (the MQTT order-update stream); the
    reconciler v1 uses cumulative ``matched`` watermarks, not per-trade rows.
    """

    model_config = _TOLERANT

    order_no: str = Field(validation_alias="orderNo")
    price: Decimal | None = Field(default=None, validation_alias="px")
    quantity: int = Field(default=0, validation_alias="qty")
    trade_id: str = Field(default="", validation_alias=AliasChoices("tradeId", "tradeNo"))
    side: str = ""
    trade_time: datetime | None = Field(default=None, validation_alias="tradeTime")

    @field_validator("order_no", "trade_id", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> Any:
        return str(value) if value is not None else value

    @field_validator("price", mode="before")
    @classmethod
    def _parse_price(cls, value: Any) -> Any:
        return _to_decimal(value)

"""Pure mapping: write payloads (Decimal-as-string, no PIN), read-side, venue classification."""

from __future__ import annotations

from typing import Any

import pytest
from src.quant_execution_engine.adapters.streaming_pro import mapping
from src.quant_execution_engine.adapters.streaming_pro.errors import StreamingProMappingError
from src.quant_execution_engine.adapters.streaming_pro.mapping import VenueOrderState
from src.quant_execution_engine.adapters.streaming_pro.models import VenueOrderRow
from src.quant_execution_engine.contracts.enums import Market, OrderType, Side

from tests.conftest import make_order


def _order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "streaming_pro", "price": "32.47"}
    payload.update(overrides)
    return make_order(**payload)


def test_paths() -> None:
    assert mapping.place_path(Market.SET).endswith("place/set")
    assert mapping.place_path(Market.TFEX).endswith("place/tfex")
    assert mapping.cancel_path() == "order/cancel"
    assert "market=TFEX" in mapping.orders_path("ACC", Market.TFEX)
    assert "account=ACC" in mapping.portfolio_path("ACC")
    assert "account=ACC" in mapping.account_path("ACC")


def test_set_payload_has_no_pin_and_string_price() -> None:
    body = mapping.to_set_payload(_order())
    assert body["side"] == "BUY" and body["price"] == "32.47" and body["price_type"] == "LIMIT"
    assert "pin" not in body and "position" not in body


def test_tfex_payload_carries_position() -> None:
    body = mapping.to_tfex_payload(_order(market="TFEX", symbol="USDM26", position_effect="OPEN"))
    assert body["position"] == "OPEN" and body["side"] == "BUY"
    assert "pin" not in body


def test_tfex_payload_without_position_raises() -> None:
    # position_effect is contract-required for TFEX; model_copy bypasses validation to hit it.
    order = _order(market="TFEX", symbol="USDM26", position_effect="OPEN").model_copy(
        update={"position_effect": None}
    )
    with pytest.raises(StreamingProMappingError):
        mapping.to_tfex_payload(order)


def test_market_order_omits_price() -> None:
    assert "price" not in mapping.to_set_payload(_order(order_type="MARKET", price=None))


def test_cancel_payload_per_market() -> None:
    tfex = mapping.to_cancel_payload(
        order_no="1", market=Market.TFEX, account="A", symbol=None, ext_order_no=None
    )
    assert tfex == {"order_no": "1", "market": "TFEX", "account": "A"}
    set_body = mapping.to_cancel_payload(
        order_no="2", market=Market.SET, account="A", symbol="PTT", ext_order_no="E1"
    )
    assert set_body["ext_order_no"] == "E1" and set_body["symbol"] == "PTT"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Buy", Side.BUY), ("Long", Side.BUY), ("Sell", Side.SELL), ("Short", Side.SELL), ("?", None)],
)
def test_from_venue_side(raw: str, expected: Side | None) -> None:
    assert mapping.from_venue_side(raw) is expected


def test_venue_row_to_normalized_set_and_unknowns() -> None:
    row = VenueOrderRow.model_validate(
        {
            "orderNo": "1",
            "symbol": "PTT",
            "side": "Buy",
            "priceType": "Limit",
            "qty": 100,
            "price": "35.00",
        }
    )
    view = mapping.venue_row_to_normalized(row, account="ACC", market=Market.SET)
    assert view is not None and view.symbol == "PTT" and view.order_type is OrderType.LIMIT
    # Unknown side / price type → None (a read view never guesses).
    bad = VenueOrderRow.model_validate({"orderNo": "2", "side": "?", "priceType": "?"})
    assert mapping.venue_row_to_normalized(bad, account="ACC", market=Market.SET) is None


def test_classify_venue_state() -> None:
    def row(**kw: Any) -> VenueOrderRow:
        return VenueOrderRow.model_validate({"orderNo": "1", **kw})

    classify = mapping.classify_venue_state
    assert classify(row(rejectReason="no margin")) is VenueOrderState.REJECTED
    assert classify(row(status="Cancelled")) is VenueOrderState.CANCELLED
    assert classify(row(showStatus="Expired")) is VenueOrderState.EXPIRED
    assert classify(row(cancelQty=1, balanceQty=0, qty=1, matchQty=0)) is VenueOrderState.CANCELLED
    assert classify(row(status="S", showStatus="Pending(S)")) is VenueOrderState.RESTING

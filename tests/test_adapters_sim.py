"""SimAdapter determinism spec."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from src.quant_execution_engine.adapters.sim import SimAdapter
from src.quant_execution_engine.contracts.enums import Broker
from src.quant_execution_engine.contracts.orders import NormalizedOrder

from tests.conftest import make_order

_FIXED_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _adapter() -> SimAdapter:
    return SimAdapter(default_fill_price=Decimal("100"), now=lambda: _FIXED_NOW)


async def test_same_input_identical_ack() -> None:
    adapter = _adapter()
    order = make_order(client_order_id="11111111-2222-4333-8444-555555555555")
    assert await adapter.place(order) == await adapter.place(order)


async def test_full_fill_default_plan_and_id_synthesis() -> None:
    order = make_order(client_order_id="11111111-2222-4333-8444-555555555555")
    ack = await _adapter().place(order)
    assert not ack.rejected
    assert ack.broker_order_id == "SIM-11111111"
    assert [f.quantity for f in ack.fills] == [order.quantity]
    assert ack.fills[0].broker_fill_id == "SIMF-11111111-1"
    assert ack.fills[0].price == order.price
    assert not ack.remainder_cancelled


async def test_reference_price_selection() -> None:
    adapter = _adapter()
    limit = await adapter.place(make_order(price="55.5"))
    assert limit.fills[0].price == Decimal("55.5")
    stop = await adapter.place(make_order(order_type="STOP", price=None, stop_price="44.25"))
    assert stop.fills[0].price == Decimal("44.25")
    market = await adapter.place(make_order(order_type="MARKET", price=None))
    assert market.fills[0].price == Decimal("100")  # sim default
    market_priced = await adapter.place(make_order(order_type="MARKET", price="9"))
    assert market_priced.fills[0].price == Decimal("9")


async def test_sim_fills_partial_full_resting_and_invalid() -> None:
    adapter = _adapter()
    partial = await adapter.place(make_order(quantity=100, metadata={"sim_fills": [40, 60]}))
    assert [f.quantity for f in partial.fills] == [40, 60]
    under = await adapter.place(make_order(quantity=100, metadata={"sim_fills": [40]}))
    assert [f.quantity for f in under.fills] == [40]
    resting = await adapter.place(make_order(metadata={"sim_fills": []}))
    assert resting.fills == ()
    for bad in ([150], [40, 70], [0], [-1], ["x"], [True], "nope"):
        ack = await adapter.place(make_order(metadata={"sim_fills": bad}))
        assert ack.rejected and ack.reject_reason is not None


async def test_sim_reject_passthrough() -> None:
    ack = await _adapter().place(make_order(metadata={"sim_reject": "venue says no"}))
    assert ack.rejected
    assert ack.reject_reason == "venue says no"


async def test_fok_requires_single_full_fill() -> None:
    adapter = _adapter()
    ok = await adapter.place(make_order(tif="FOK"))
    assert not ok.rejected
    bad = await adapter.place(make_order(tif="FOK", metadata={"sim_fills": [40, 60]}))
    assert bad.rejected and "FOK" in (bad.reject_reason or "")


async def test_ioc_flags_unfilled_remainder() -> None:
    adapter = _adapter()
    partial = await adapter.place(make_order(tif="IOC", quantity=100, metadata={"sim_fills": [40]}))
    assert partial.remainder_cancelled
    empty = await adapter.place(make_order(tif="IOC", metadata={"sim_fills": []}))
    assert empty.remainder_cancelled
    full = await adapter.place(make_order(tif="IOC"))
    assert not full.remainder_cancelled


async def test_interface_complete_unexposed_methods() -> None:
    adapter = _adapter()
    assert (await adapter.cancel("x")).ok
    amend = await adapter.amend("x", new_price=Decimal("1"), new_qty=1)
    assert amend.ok and amend.semantics == "native"
    assert await adapter.get_open_orders("ACC") == []
    assert await adapter.get_positions("ACC") == []
    account = await adapter.get_account("ACC")
    assert account.buying_power > 0
    assert await adapter.heartbeat() is True


def test_capabilities_are_the_sim_rows() -> None:
    rows = _adapter().capabilities()
    assert {entry.market for entry in rows} == {"SET", "TFEX"}
    assert all(entry.broker is Broker.SIM and entry.adapter_installed for entry in rows)


# ----------------------------------------------------- price-source injection


class _FixedPriceSource:
    """A FillPriceSource that returns a configured price (or None)."""

    def __init__(self, price: Decimal | None) -> None:
        self.price = price
        self.calls: list[str] = []

    async def fill_price(self, order: NormalizedOrder) -> Decimal | None:
        self.calls.append(order.client_order_id)
        return self.price


async def test_price_source_price_used_for_all_fills() -> None:
    source = _FixedPriceSource(Decimal("77.25"))
    adapter = SimAdapter(
        default_fill_price=Decimal("100"), now=lambda: _FIXED_NOW, price_source=source
    )
    ack = await adapter.place(make_order(quantity=100, metadata={"sim_fills": [40, 60]}))
    assert [f.price for f in ack.fills] == [Decimal("77.25"), Decimal("77.25")]
    assert len(source.calls) == 1  # priced once, applied to the whole plan


async def test_price_source_none_falls_back_to_reference_price() -> None:
    source = _FixedPriceSource(None)
    adapter = SimAdapter(
        default_fill_price=Decimal("100"), now=lambda: _FIXED_NOW, price_source=source
    )
    ack = await adapter.place(make_order(price="55.5"))
    assert ack.fills[0].price == Decimal("55.5")  # the order's LIMIT reference


async def test_price_source_absent_is_unchanged_phase2_behavior() -> None:
    # No price_source ⇒ bit-for-bit the reference-price path.
    plain = await _adapter().place(make_order(price="55.5"))
    assert plain.fills[0].price == Decimal("55.5")

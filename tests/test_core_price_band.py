"""Price-band advisory pre-trade check (Phase 6 / A2)."""

from __future__ import annotations

import logging

import pytest
import respx
from src.quant_execution_engine.adapters.market_data import MarketDataClient
from src.quant_execution_engine.contracts.errors import PriceBandExceeded
from src.quant_execution_engine.core.price_band import PriceBandCheck

from tests.conftest import make_order, make_settings

_BASE = "http://quant-marketdata-engine:8000"
_OHLCV = f"{_BASE}/ohlcv"


def _check(*, base_url: str | None = _BASE, **overrides: object) -> PriceBandCheck:
    settings = make_settings(price_band_enabled=True, **overrides)
    return PriceBandCheck(settings, MarketDataClient(base_url, None))


def _close(value: str) -> dict[str, object]:
    return {"bars": [{"ts": "2026-06-11T00:00:00Z", "close": value}]}


@respx.mock
async def test_within_band_passes() -> None:
    respx.get(_OHLCV).respond(json=_close("100.0"))
    check = _check(price_band_max_pct="10.0")
    await check.check(make_order(symbol="PTT", price="109"))  # +9% < 10% ⇒ passes
    await check._market_data.aclose()


@respx.mock
async def test_outside_band_rejects_422() -> None:
    respx.get(_OHLCV).respond(json=_close("100.0"))
    check = _check(price_band_max_pct="10.0")
    with pytest.raises(PriceBandExceeded) as exc:
        await check.check(make_order(symbol="PTT", price="120"))  # +20% > 10%
    assert exc.value.code == "price_band_exceeded"
    assert exc.value.detail["symbol"] == "PTT"
    assert exc.value.detail["price"] == "120"
    assert exc.value.detail["last_close"] == "100.0"
    await check._market_data.aclose()


@respx.mock
async def test_band_is_symmetric_below_close() -> None:
    respx.get(_OHLCV).respond(json=_close("100.0"))
    check = _check(price_band_max_pct="10.0")
    with pytest.raises(PriceBandExceeded):
        await check.check(make_order(symbol="PTT", side="SELL", price="80"))  # -20%
    await check._market_data.aclose()


@respx.mock
async def test_boundary_exactly_at_max_pct_passes() -> None:
    respx.get(_OHLCV).respond(json=_close("100.0"))
    check = _check(price_band_max_pct="10.0")
    await check.check(make_order(symbol="PTT", price="110"))  # exactly +10% ⇒ not > ⇒ pass
    await check._market_data.aclose()


async def test_market_order_bypasses_band() -> None:
    # No HTTP route registered: a MARKET order must never fetch (no limit to band).
    check = _check(price_band_max_pct="10.0")
    await check.check(make_order(symbol="PTT", order_type="MARKET", price=None))
    await check._market_data.aclose()


@respx.mock
async def test_fetch_failure_warns_and_passes(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(_OHLCV).respond(status_code=500)
    check = _check(price_band_max_pct="1.0")  # tight band, but the fetch fails
    with caplog.at_level(logging.WARNING, logger="src.quant_execution_engine.core.price_band"):
        await check.check(make_order(symbol="PTT", price="999"))  # would breach if fetched
    assert any("price_band.skipped_no_reference" in r.message for r in caplog.records)
    await check._market_data.aclose()


@respx.mock
async def test_non_positive_last_close_warns_and_passes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get(_OHLCV).respond(json=_close("0"))
    check = _check(price_band_max_pct="1.0")
    with caplog.at_level(logging.WARNING, logger="src.quant_execution_engine.core.price_band"):
        await check.check(make_order(symbol="PTT", price="999"))
    assert any("non_positive_last_close" in r.message for r in caplog.records)
    await check._market_data.aclose()


async def test_disabled_flag_skips_entirely() -> None:
    # Enabled=False ⇒ no fetch even with a base URL (no HTTP route registered).
    settings = make_settings(price_band_enabled=False)
    check = PriceBandCheck(settings, MarketDataClient(_BASE, None))
    assert check.enabled is False
    await check.check(make_order(symbol="PTT", price="99999"))
    await check._market_data.aclose()


async def test_no_base_url_skips_entirely() -> None:
    # Enabled but no market-data URL ⇒ unconfigured ⇒ no-op (no HTTP route).
    check = _check(base_url=None, price_band_max_pct="0.01")
    assert check.enabled is False
    await check.check(make_order(symbol="PTT", price="99999"))
    await check._market_data.aclose()

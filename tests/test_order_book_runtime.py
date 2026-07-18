"""Order-book runtime tests: disabled-by-default no-op, enabled lifecycle."""

from __future__ import annotations

from pydantic import SecretStr
from src.quant_execution_engine.order_book import runtime as ob_runtime
from src.quant_execution_engine.order_book.models import OrderBookSource

from tests.conftest import make_settings


async def test_disabled_by_default_is_noop() -> None:
    settings = make_settings()  # order_book_enabled defaults False
    assert ob_runtime.order_book_enabled(settings) is False
    assert ob_runtime.create_order_book_runtime(settings) is None
    await ob_runtime.start_order_book_workers(settings)  # no-op
    assert ob_runtime.get_order_book_service() is None
    await ob_runtime.close_order_book_runtime()  # idempotent


async def test_enabled_without_provider_stays_disabled() -> None:
    settings = make_settings(order_book_enabled=True)
    assert ob_runtime.order_book_enabled(settings) is False
    assert ob_runtime.create_order_book_runtime(settings) is None


async def test_enabled_with_liberator_key_creates_service() -> None:
    settings = make_settings(
        order_book_enabled=True,
        order_book_primary_provider="liberator",
        liberator_api_key=SecretStr("k"),
    )
    assert ob_runtime.order_book_enabled(settings) is True
    service = ob_runtime.create_order_book_runtime(settings)
    assert service is not None
    assert ob_runtime.get_order_book_service() is service
    # Calling again returns the same singleton.
    assert ob_runtime.create_order_book_runtime(settings) is service
    await ob_runtime.start_order_book_workers(settings)
    await ob_runtime.close_order_book_runtime()
    assert ob_runtime.get_order_book_service() is None


async def test_symbol_override_parsed_when_provider_configured() -> None:
    # A valid provider name is kept; an unknown name is dropped.
    settings = make_settings(
        order_book_enabled=True,
        order_book_primary_provider="liberator",
        liberator_api_key=SecretStr("k"),
        order_book_symbol_overrides={"AOT": "liberator", "PTT": "bogus"},
    )
    service = ob_runtime.create_order_book_runtime(settings)
    assert service is not None
    assert ob_runtime._router is not None
    assert ob_runtime._router.active is OrderBookSource.LIBERATOR
    # "bogus" is not a known provider, so the PTT override is dropped.
    assert ob_runtime._router._overrides == {"AOT": OrderBookSource.LIBERATOR}
    await ob_runtime.close_order_book_runtime()

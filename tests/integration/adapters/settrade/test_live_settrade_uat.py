"""Live UAT-sandbox checks against the Settrade Open API v2 (owner mode).

Settrade is a cloud API (no local overlay) — these run against the UAT sandbox
(broker ``098`` on ``https://open-api-test.settrade.com``). Keyed off the
``EXECUTION_ENGINE_SETTRADE_*`` env vars: every test skips when the credentials
are absent, so CI (which has none) never reaches the wire.

Run explicitly (excluded by default): ``uv run pytest -m integration --no-cov``.
The full UAT order flow (place far-from-market → amend → cancel) is the operator
runbook in ``.claude/playbooks/order-routing-safety.md`` ("Settrade specifics").
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from pydantic import SecretStr
from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
from src.quant_execution_engine.adapters.settrade.client import SettradeClient
from src.quant_execution_engine.contracts.enums import Market

pytestmark = pytest.mark.integration

_ENV = "EXECUTION_ENGINE_SETTRADE_"
_REQUIRED = (
    f"{_ENV}APP_ID",
    f"{_ENV}APP_SECRET",
    f"{_ENV}APP_CODE",
    f"{_ENV}BROKER_ID",
    f"{_ENV}PIN",
    f"{_ENV}ACCOUNT_NO",  # the integration-test account (NOT required to enable the broker)
)


def _credentials() -> dict[str, str]:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        pytest.skip(f"settrade UAT creds absent: {', '.join(missing)}")
    return {name: os.environ[name] for name in _REQUIRED}


def _adapter(creds: dict[str, str]) -> SettradeAdapter:
    base_url = os.environ.get(f"{_ENV}BASE_URL", "https://open-api-test.settrade.com")
    client = SettradeClient(
        base_url=base_url,
        app_id=SecretStr(creds[f"{_ENV}APP_ID"]),
        app_secret=SecretStr(creds[f"{_ENV}APP_SECRET"]),
        app_code=creds[f"{_ENV}APP_CODE"],
        broker_id=creds[f"{_ENV}BROKER_ID"],
    )
    return SettradeAdapter(
        client=client,
        broker_id=creds[f"{_ENV}BROKER_ID"],
        pin=SecretStr(creds[f"{_ENV}PIN"]),
    )


async def test_uat_token_acquire_and_account_read() -> None:
    """ensure_token() succeeds and get_account() returns a buying-power figure."""
    creds = _credentials()
    adapter = _adapter(creds)
    try:
        assert await adapter.heartbeat() is True
        account = await adapter.get_account(creds[f"{_ENV}ACCOUNT_NO"])
        assert account.buying_power >= Decimal("0")
    finally:
        await adapter.aclose()


async def test_uat_place_amend_cancel_far_from_market() -> None:
    """Outline: place a far-from-market LIMIT, amend its price, then cancel it.

    Left as a documented skeleton (the operator drives the real order in the
    safety runbook) — asserting a real placement here would leave a resting
    order on the sandbox book. The flow it exercises:

        place(LIMIT far from market) → amend(price) → cancel → fetch_venue_orders
    """
    creds = _credentials()
    adapter = _adapter(creds)
    try:
        # Reads are side-effect-free and safe to assert against the live book.
        orders = await adapter.fetch_venue_orders(creds[f"{_ENV}ACCOUNT_NO"], Market.SET)
        assert isinstance(orders, list)
    finally:
        await adapter.aclose()

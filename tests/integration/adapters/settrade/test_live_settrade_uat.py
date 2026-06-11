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
        clients={Market.SET: client, Market.TFEX: client},
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


# ----------------------------- Phase 4.1: real InnovestX per-market read-only ---

# Opt-in: requires the SPLIT InnovestX OAuth apps (broker 023, prod). Skips unless
# BOTH per-market trios are present. Reads only — the PIN is a placeholder because
# get_account/get_positions never serialize it (it enters write payloads only).
_INNOVESTX_EQUITY = (
    f"{_ENV}EQUITY_APP_ID",
    f"{_ENV}EQUITY_APP_SECRET",
    f"{_ENV}EQUITY_APP_CODE",
)
_INNOVESTX_DERIVATIVES = (
    f"{_ENV}DERIVATIVES_APP_ID",
    f"{_ENV}DERIVATIVES_APP_SECRET",
    f"{_ENV}DERIVATIVES_APP_CODE",
)
_INNOVESTX_SET_ACCOUNT = "902001825"
_INNOVESTX_TFEX_ACCOUNT = "507619-0"


def _innovestx_dual_adapter() -> SettradeAdapter:
    needed = (*_INNOVESTX_EQUITY, *_INNOVESTX_DERIVATIVES, f"{_ENV}BROKER_ID")
    missing = [name for name in needed if not os.environ.get(name)]
    if missing:
        pytest.skip(f"InnovestX per-market creds absent: {', '.join(missing)}")
    base_url = os.environ.get(f"{_ENV}BASE_URL", "https://open-api.settrade.com")
    broker_id = os.environ[f"{_ENV}BROKER_ID"]

    def _client(prefix: tuple[str, str, str]) -> SettradeClient:
        return SettradeClient(
            base_url=base_url,
            app_id=SecretStr(os.environ[prefix[0]]),
            app_secret=SecretStr(os.environ[prefix[1]]),
            app_code=os.environ[prefix[2]],
            broker_id=broker_id,
        )

    return SettradeAdapter(
        clients={
            Market.SET: _client(_INNOVESTX_EQUITY),
            Market.TFEX: _client(_INNOVESTX_DERIVATIVES),
        },
        broker_id=broker_id,
        pin=SecretStr("000000"),  # placeholder — reads never serialize the PIN
    )


async def test_innovestx_per_market_reads_through_split_apps() -> None:
    """get_account/get_positions for the equity (SET) + derivatives (TFEX) books.

    902001825 reads through the equity (ALGO_EQ) client; 507619-0 reads through
    the derivatives (ALGO) client — proving the refactored adapter routes each
    market to its own OAuth app against real InnovestX (broker 023). No writes.
    """
    adapter = _innovestx_dual_adapter()
    try:
        assert await adapter.heartbeat() is True  # both apps' tokens acquirable
        set_account = await adapter.get_account(_INNOVESTX_SET_ACCOUNT)
        assert set_account.buying_power >= Decimal("0")
        set_positions = await adapter.get_positions(_INNOVESTX_SET_ACCOUNT)
        assert isinstance(set_positions, list)
        tfex_positions = await adapter.get_positions(_INNOVESTX_TFEX_ACCOUNT)
        assert isinstance(tfex_positions, list)
    finally:
        await adapter.aclose()

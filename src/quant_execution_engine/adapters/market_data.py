"""Shared read-only market-data last-close fetcher.

A single small async client over the market-data engine's ``GET /ohlcv`` last
``1d`` bar, used by BOTH the ``SimAdapter`` fill-price chain (hop 2, D21) and the
Phase-6 price-band pre-trade check (A2). Factored out so the httpx + parse logic
lives in one place — neither caller duplicates it.

The client owns a lazily-created ``httpx.AsyncClient`` when one is not injected.
The market-data API key is a ``SecretStr`` sent as ``X-API-Key`` and is NEVER
logged. Any failure (HTTP error, empty/bad payload) returns ``None`` after a
WARNING — the callers decide what a miss means (sim falls through, the band
check passes advisorily).
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import SecretStr

from src.quant_execution_engine.config.settings import Settings
from src.quant_execution_engine.contracts.enums import Market

logger = logging.getLogger(__name__)

_MARKET_DATA_TIMEOUT_SECONDS = 2.0


class MarketDataClient:
    """Fetch a symbol's last ``1d`` close from the market-data engine."""

    def __init__(
        self,
        base_url: str | None,
        api_key: SecretStr | None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._http = http
        self._owns_http = http is None

    @property
    def configured(self) -> bool:
        """True when a base URL is set (the fetch hop is otherwise skipped)."""
        return self._base_url is not None

    def _client(self) -> httpx.AsyncClient:
        """Lazily build the owned client (only if not injected)."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=_MARKET_DATA_TIMEOUT_SECONDS)
        return self._http

    async def aclose(self) -> None:
        """Close the owned client (a no-op when one was injected)."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def last_close(self, symbol: str, market: Market) -> Decimal | None:
        """Return the latest ``1d`` close for ``MARKET:SYMBOL``, or ``None``.

        ``None`` is returned (after a WARNING) when the hop is unconfigured or
        ANY fetch/parse step fails — the call is advisory, never raising to the
        caller.
        """
        if self._base_url is None:
            return None
        prefixed = f"{market.value}:{symbol}"
        params: dict[str, str | int] = {"symbol": prefixed, "timeframe": "1d", "limit": 1}
        headers: dict[str, str] = {}
        if self._api_key is not None:
            headers["X-API-Key"] = self._api_key.get_secret_value()
        try:
            response = await self._client().get(
                f"{self._base_url}/ohlcv", params=params, headers=headers
            )
            response.raise_for_status()
            return _latest_close(response.json())
        except (httpx.HTTPError, ValueError, KeyError, InvalidOperation) as exc:
            logger.warning(
                "market_data.last_close_failed symbol=%s reason=%s",
                symbol,
                exc.__class__.__name__,
            )
            return None


def _latest_close(payload: object) -> Decimal:
    """Parse the max-``ts`` bar's ``close`` as ``Decimal``; raise on empty/bad."""
    if not isinstance(payload, dict):
        raise ValueError("market-data response is not an object")
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars:
        raise ValueError("market-data response carried no bars")
    latest = max(bars, key=lambda bar: str(bar["ts"]))
    return Decimal(str(latest["close"]))


# Process-singleton client for the price-band check (A2). The router is built
# per-request, so it must NOT own a client (that would leak one httpx pool per
# request); it borrows this singleton, created in the lifespan and closed once.
_client: MarketDataClient | None = None


def create_market_data_client(settings: Settings) -> MarketDataClient:
    """Create (or return) the process-singleton market-data client."""
    global _client
    if _client is None:
        _client = MarketDataClient(settings.market_data_base_url, settings.market_data_api_key)
    return _client


def get_market_data_client() -> MarketDataClient | None:
    """The singleton client, or ``None`` before the lifespan created it."""
    return _client


async def close_market_data_client() -> None:
    """Close + clear the singleton client (idempotent)."""
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


__all__ = [
    "MarketDataClient",
    "close_market_data_client",
    "create_market_data_client",
    "get_market_data_client",
]

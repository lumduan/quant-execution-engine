"""Redacting httpx transport for the bundled settrade-streaming-api bridge.

The ONLY module that touches the bridge wire. Secret hygiene (hard rule 3): the
``X-API-Key`` header comes from a ``SecretStr``; log lines carry method/path/
status plus a redacted payload — the account number never reaches a log record.
The engine holds NO PIN (the bridge stamps it), so no PIN ever crosses this wire.

httpx gotcha (load-bearing): with ``base_url = ".../api/v1"`` a LEADING-slash
path would REPLACE ``/api/v1``. The transport normalizes ``base_url`` to a
trailing slash and joins paths relative (no leading slash).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from src.quant_execution_engine.adapters.streaming_pro.errors import StreamingProTransportError

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = frozenset({"pin", "accountno", "account", "account_no", "password", "api_key"})
_REDACTED = "***"


def redact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Mask secret/PII values (account number, any stray pin) for logging."""
    return {
        key: _REDACTED if key.lower() in _SENSITIVE_KEYS else value
        for key, value in payload.items()
    }


class StreamingProTransport:
    """Thin async HTTP client: JSON I/O + typed transport failures.

    ``post``/``get_json`` return the parsed body for 2xx AND 4xx (a 4xx carries
    the bridge's ``{detail}`` — a structured rejection, not breaker food); only
    connectivity, timeouts, 5xx, and non-JSON raise :class:`StreamingProTransportError`.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _url(self, path: str) -> str:
        return self._base_url + path.lstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key.get_secret_value()}

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one JSON body; return the parsed object body (4xx included)."""
        logger.debug("streaming_pro POST %s payload=%s", path, redact_payload(payload))
        try:
            response = await self._client.post(
                self._url(path), json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise StreamingProTransportError(f"streaming_pro POST {path} failed: {exc!r}") from exc
        logger.debug("streaming_pro POST %s -> %d", path, response.status_code)
        if response.status_code >= 500:
            raise StreamingProTransportError(
                f"streaming_pro POST {path} upstream error HTTP {response.status_code}"
            )
        body = self._json(path, response)
        if not isinstance(body, dict):
            raise StreamingProTransportError(
                f"streaming_pro POST {path} returned a non-object body"
            )
        return body

    async def get_json(self, path: str) -> Any:
        """GET one JSON document (orders / portfolio / account / session)."""
        logger.debug("streaming_pro GET %s", path)
        try:
            response = await self._client.get(self._url(path), headers=self._headers())
        except httpx.HTTPError as exc:
            raise StreamingProTransportError(f"streaming_pro GET {path} failed: {exc!r}") from exc
        logger.debug("streaming_pro GET %s -> %d", path, response.status_code)
        if response.status_code >= 500:
            raise StreamingProTransportError(
                f"streaming_pro GET {path} upstream error HTTP {response.status_code}"
            )
        return self._json(path, response)

    @staticmethod
    def _json(path: str, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise StreamingProTransportError(
                f"streaming_pro {path} returned non-JSON (HTTP {response.status_code})"
            ) from exc

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

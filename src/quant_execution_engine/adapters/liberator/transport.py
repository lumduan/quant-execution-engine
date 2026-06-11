"""Redacting httpx transport for the bundled liberator-trading-api upstream.

The ONLY module that touches the Liberator wire. Secret hygiene (hard rule 3):
the api-key header comes from a ``SecretStr`` and is attached per request;
log lines carry method/path/status plus a redacted payload — the PIN and the
account number never reach a log record.

httpx gotcha (load-bearing): with ``base_url = ".../api/v1"`` a LEADING-slash
path would REPLACE ``/api/v1`` entirely. The transport therefore normalizes
``base_url`` to a trailing slash and joins paths relative (no leading slash).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from src.quant_execution_engine.adapters.liberator.errors import LiberatorTransportError
from src.quant_execution_engine.adapters.liberator.models import LiberatorEnvelope

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = frozenset({"pin", "accountno", "account_no", "password", "apitoken", "api_key"})
_REDACTED = "***"


def redact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Mask secret/PII values (PIN, account number, tokens) for logging."""
    return {
        key: _REDACTED if key.lower() in _SENSITIVE_KEYS else value
        for key, value in payload.items()
    }


class LiberatorTransport:
    """Thin async HTTP client: envelope parsing + typed transport failures."""

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
        return {"api-key": self._api_key.get_secret_value()}

    async def post(self, path: str, payload: dict[str, Any]) -> LiberatorEnvelope:
        """POST one JSON body; return the parsed envelope (4xx included).

        A structured upstream rejection is an envelope with ``ok == False`` —
        the venue's reason travels onward. Connectivity, timeouts, 5xx, and
        non-JSON bodies raise :class:`LiberatorTransportError` (breaker food).
        """
        logger.debug("liberator POST %s payload=%s", path, redact_payload(payload))
        try:
            response = await self._client.post(
                self._url(path), json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise LiberatorTransportError(f"liberator POST {path} failed: {exc!r}") from exc
        logger.debug("liberator POST %s -> %d", path, response.status_code)
        if response.status_code >= 500:
            raise LiberatorTransportError(
                f"liberator POST {path} upstream error HTTP {response.status_code}"
            )
        return self._parse_envelope(path, response)

    async def get_json(self, path: str) -> dict[str, Any]:
        """GET one JSON document (orders query, health probes)."""
        logger.debug("liberator GET %s", path)
        try:
            response = await self._client.get(self._url(path), headers=self._headers())
        except httpx.HTTPError as exc:
            raise LiberatorTransportError(f"liberator GET {path} failed: {exc!r}") from exc
        logger.debug("liberator GET %s -> %d", path, response.status_code)
        if response.status_code >= 500:
            raise LiberatorTransportError(
                f"liberator GET {path} upstream error HTTP {response.status_code}"
            )
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise LiberatorTransportError(f"liberator GET {path} returned non-JSON") from exc
        return body

    def _parse_envelope(self, path: str, response: httpx.Response) -> LiberatorEnvelope:
        try:
            body = response.json()
        except ValueError as exc:
            raise LiberatorTransportError(
                f"liberator POST {path} returned non-JSON (HTTP {response.status_code})"
            ) from exc
        if not isinstance(body, dict):
            raise LiberatorTransportError(
                f"liberator POST {path} returned a non-object body (HTTP {response.status_code})"
            )
        return LiberatorEnvelope.model_validate(body)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

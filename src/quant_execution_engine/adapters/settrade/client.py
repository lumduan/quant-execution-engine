"""Settrade Open API v2 OAuth transport — the ONLY module touching the wire.

Re-implements the ``settrade-v2`` SDK auth recipe async (Design Decision 2):
ECDSA P-256 login signature, single-flight token acquisition under one lock,
proactive refresh inside a margin, refresh-fail->login fallback (the SDK silently
ignores refresh failure — a bug we do NOT copy), and exactly one reactive 401
re-auth guarded by a token serial. Secret hygiene (hard rule 3):
``app_secret``/PIN/tokens/signature/account numbers NEVER reach a log record.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from src.quant_execution_engine.adapters.settrade.errors import (
    SettradeAuthError,
    SettradeTransportError,
    SettradeVenueRejection,
)
from src.quant_execution_engine.adapters.settrade.models import (
    SettradeErrorBody,
    SettradeTokenResponse,
)

logger = logging.getLogger(__name__)

# Secret hygiene (hard rule 3): the Settrade account number rides the URL PATH
# (``.../accounts/{account_no}/...``), and httpx's own request logger emits the
# full URL at INFO. We redact the account in OUR log lines, but cannot redact a
# third-party logger's content — so quiet httpx's request log to WARNING here so
# an account number never reaches a record through it. (Transport failures still
# surface as typed errors with a redacted path.)
logging.getLogger("httpx").setLevel(logging.WARNING)

_REDACTED = "***"
_SENSITIVE_KEYS = frozenset(
    {
        "pin",
        "apikey",
        "api_key",
        "signature",
        "refreshtoken",
        "refresh_token",
        "accesstoken",
        "access_token",
        "token",
        "password",
    }
)


def sign_content(app_secret: SecretStr, content: str) -> str:
    """ECDSA-SHA256 sign ``content`` with the base64-encoded EC P-256 secret.

    Exactly the SDK recipe (``settrade_v2/util.create_sha256_with_ecdsa_signature``):
    base64-decode the secret to the raw private scalar, derive the SECP256R1 key,
    sign ``content``'s bytes, and return the signature hex. The secret never leaves
    this function.
    """
    raw = base64.b64decode(app_secret.get_secret_value())
    private_key = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    signature = private_key.sign(content.encode(), ec.ECDSA(hashes.SHA256()))
    return signature.hex()


def redact_payload(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Mask secret values (case-insensitive key match) for logging."""
    return {
        key: _REDACTED if key.lower() in _SENSITIVE_KEYS else value
        for key, value in mapping.items()
    }


def redact_path(path: str) -> str:
    """Mask the account-number segment (the part after ``accounts/``)."""
    marker = "accounts/"
    idx = path.find(marker)
    if idx == -1:
        return path
    start = idx + len(marker)
    end = path.find("/", start)
    tail = path[end:] if end != -1 else ""
    return path[:start] + _REDACTED + tail


@dataclass
class RateBudget:
    """A parsed rate-limit snapshot for one bucket (GET or WRITE)."""

    remaining_second: int | None = None
    remaining_minute: int | None = None
    limit_second: int | None = None
    limit_minute: int | None = None


class SettradeClient:
    """Async OAuth transport for Settrade Open API v2.

    The single module that holds the OAuth session. Token state is private and
    never surfaces through ``__repr__``. Auth requests bypass ``request_json`` (no
    token needed) via :meth:`_raw_send`, but still update ``last_wire_ok`` and the
    rate buckets so a dead OAM endpoint trips the breaker like any other failure.
    """

    def __init__(
        self,
        *,
        base_url: str,
        app_id: SecretStr,
        app_secret: SecretStr,
        app_code: str,
        broker_id: str,
        refresh_margin_seconds: int = 100,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._app_code = app_code
        self._broker_id = broker_id
        self._refresh_margin = refresh_margin_seconds
        self._now = now
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

        self._token_type: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._token_serial: int = 0
        self._lock = asyncio.Lock()
        self.last_wire_ok: bool | None = None  # health surface (never secret)
        self._rate: dict[str, RateBudget] = {}

    def __repr__(self) -> str:
        return (
            f"SettradeClient(base_url={self._base_url!r}, broker_id={self._broker_id!r}, "
            f"app_code={self._app_code!r})"
        )

    def _login_url(self) -> str:
        return f"{self._base_url}/api/oam/v1/{self._broker_id}/broker-apps/{self._app_code}/login"

    def _refresh_url(self) -> str:
        return (
            f"{self._base_url}/api/oam/v1/{self._broker_id}"
            f"/broker-apps/{self._app_code}/refresh-token"
        )

    def _api_url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    @staticmethod
    def _bucket(method: str) -> str:
        """GET vs WRITE buckets (POST+PATCH share — SDK ``rate_limit_id``)."""
        return "GET" if method.upper() == "GET" else "WRITE"

    @staticmethod
    def _header_int(response: httpx.Response, name: str) -> int | None:
        value = response.headers.get(name)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _record_rate(self, method: str, response: httpx.Response) -> None:
        budget = RateBudget(
            remaining_second=self._header_int(response, "X-RateLimit-Remaining-second"),
            remaining_minute=self._header_int(response, "X-RateLimit-Remaining-minute"),
            limit_second=self._header_int(response, "X-RateLimit-Limit-second"),
            limit_minute=self._header_int(response, "X-RateLimit-Limit-minute"),
        )
        bucket = self._bucket(method)
        self._rate[bucket] = budget
        if budget.remaining_second == 0 or budget.remaining_minute == 0:
            logger.warning("settrade rate budget exhausted bucket=%s", bucket)

    def rate_snapshot(self) -> dict[str, RateBudget]:
        """Current per-bucket rate budgets (``{"GET": ..., "WRITE": ...}``)."""
        return dict(self._rate)

    async def _raw_send(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request; record wire health + rate budget; raise on transport.

        A successful HTTP exchange (any status, incl. 4xx) sets ``last_wire_ok =
        True``; a connect/timeout/transport error sets it ``False`` and raises
        :class:`SettradeTransportError` (breaker food).
        """
        try:
            response = await self._client.request(method, url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            self.last_wire_ok = False
            raise SettradeTransportError(
                f"settrade {method} {redact_path(url)} failed: {exc!r}"
            ) from exc
        self.last_wire_ok = True
        self._record_rate(method, response)
        return response

    async def ensure_token(self, *, force: bool = False) -> None:
        """Acquire/refresh the token under the single-flight lock.

        No token (or ``force``) -> login; inside the refresh margin -> refresh,
        falling back to a fresh login on ANY refresh failure (the SDK silently
        ignores refresh failure — we do NOT copy that bug). A still-valid token is
        reused untouched.
        """
        async with self._lock:
            if force or self._access_token is None:
                await self._login()
                return
            if self._expires_at - self._now() <= self._refresh_margin:
                try:
                    await self._refresh()
                except SettradeAuthError:
                    logger.info("settrade refresh failed; falling back to login")
                    await self._login()

    def _parse_token(self, response: httpx.Response, *, what: str) -> SettradeTokenResponse:
        if response.status_code >= 300 or response.status_code < 200:
            code, message = _auth_failure_fields(response)
            raise SettradeAuthError(f"settrade {what} failed: {code}: {message}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SettradeAuthError(f"settrade {what} returned non-JSON") from exc
        try:
            return SettradeTokenResponse.model_validate(body)
        except ValueError as exc:
            raise SettradeAuthError(
                f"settrade {what} returned an unexpected body: {exc!r}"
            ) from exc

    def _store_token(self, token: SettradeTokenResponse) -> None:
        self._token_type = token.token_type
        self._access_token = token.access_token
        self._refresh_token = token.refresh_token
        self._expires_at = self._now() + token.expires_in
        self._token_serial += 1

    async def _login(self) -> None:
        ts = str(int(self._now() * 1000))
        params = ""
        content = f"{self._app_id.get_secret_value()}.{params}.{ts}"
        body = {
            "apiKey": self._app_id.get_secret_value(),
            "params": params,
            "signature": sign_content(self._app_secret, content),
            "timestamp": ts,
        }
        response = await self._raw_send("POST", self._login_url(), json=body)
        self._store_token(self._parse_token(response, what="login"))
        logger.info("settrade token acquired")

    async def _refresh(self) -> None:
        if self._refresh_token is None:
            raise SettradeAuthError("settrade refresh requested with no refresh token")
        body = {
            "apiKey": self._app_id.get_secret_value(),
            "refreshToken": self._refresh_token,
        }
        response = await self._raw_send("POST", self._refresh_url(), json=body)
        self._store_token(self._parse_token(response, what="refresh"))
        logger.info("settrade token refreshed")

    def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"{self._token_type} {self._access_token}"}

    async def request_json(
        self, method: str, path: str, payload: dict[str, Any] | list[Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """Send an authenticated request and return the parsed JSON.

        ``ensure_token()`` runs first (outside the per-request serial capture).
        On a reactive 401, if the token serial is unchanged since the request
        started, force exactly one re-auth and retry once; a second 401 raises
        :class:`SettradeAuthError`. Response policy: 2xx empty/non-JSON -> ``{}``;
        2xx JSON -> parsed; structured non-2xx ``{code, message}`` with status
        < 500 -> :class:`SettradeVenueRejection`; >=500/transport/non-JSON-error
        -> :class:`SettradeTransportError`.
        """
        await self.ensure_token()
        serial = self._token_serial
        response = await self._send_authenticated(method, path, payload)
        if response.status_code == 401:
            await self._reauth_after_401(serial)
            response = await self._send_authenticated(method, path, payload)
            if response.status_code == 401:
                raise SettradeAuthError(f"settrade {method} {redact_path(path)} 401 after re-auth")
        return self._parse_response(method, path, response)

    async def _send_authenticated(
        self, method: str, path: str, payload: dict[str, Any] | list[Any] | None
    ) -> httpx.Response:
        if isinstance(payload, Mapping):
            logger.debug(
                "settrade %s %s payload=%s", method, redact_path(path), redact_payload(payload)
            )
        else:
            logger.debug("settrade %s %s", method, redact_path(path))
        response = await self._raw_send(
            method, self._api_url(path), json=payload, headers=self._auth_header()
        )
        logger.debug("settrade %s %s -> %d", method, redact_path(path), response.status_code)
        return response

    async def _reauth_after_401(self, serial: int) -> None:
        async with self._lock:
            if self._token_serial != serial:
                return  # another caller already re-authed; reuse it
            try:
                await self._refresh()
            except SettradeAuthError:
                await self._login()

    def _parse_response(
        self, method: str, path: str, response: httpx.Response
    ) -> dict[str, Any] | list[Any]:
        status = response.status_code
        if 200 <= status < 300:
            if not response.content:
                return {}
            try:
                body: dict[str, Any] | list[Any] = response.json()
            except ValueError:
                return {}
            return body
        if status >= 500:
            raise SettradeTransportError(f"settrade {method} {redact_path(path)} HTTP {status}")
        try:
            raw = response.json()
        except ValueError as exc:
            raise SettradeTransportError(
                f"settrade {method} {redact_path(path)} non-JSON error HTTP {status}"
            ) from exc
        try:
            err = SettradeErrorBody.model_validate(raw)
        except ValueError as exc:
            raise SettradeTransportError(
                f"settrade {method} {redact_path(path)} unstructured error HTTP {status}"
            ) from exc
        raise SettradeVenueRejection(err.code, status, err.message)

    async def get_json(self, path: str) -> dict[str, Any] | list[Any]:
        """GET one JSON document."""
        return await self.request_json("GET", path)

    async def post_json(
        self, path: str, payload: dict[str, Any] | list[Any]
    ) -> dict[str, Any] | list[Any]:
        """POST one JSON body."""
        return await self.request_json("POST", path, payload)

    async def patch_json(
        self, path: str, payload: dict[str, Any] | list[Any]
    ) -> dict[str, Any] | list[Any]:
        """PATCH one JSON body (native amend / cancel)."""
        return await self.request_json("PATCH", path, payload)

    async def aclose(self) -> None:
        """Close the owned httpx client (a no-op for an injected client)."""
        if self._owns_client:
            await self._client.aclose()


def _auth_failure_fields(response: httpx.Response) -> tuple[str, str]:
    """Extract ``(code, message)`` from a failed auth response WITHOUT echoing creds.

    Only the venue's structured ``code``/``message`` fields are surfaced; the raw
    body text is never included (it could echo a signature or token).
    """
    try:
        raw = response.json()
    except ValueError:
        return str(response.status_code), "non-JSON auth error"
    if isinstance(raw, dict):
        return str(raw.get("code", response.status_code)), str(raw.get("message", "auth error"))
    return str(response.status_code), "auth error"

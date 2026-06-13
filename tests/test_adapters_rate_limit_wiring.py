"""D1/D2 wiring: the Settrade GET/WRITE buckets + the Liberator place bucket.

Proves each adapter acquires the CORRECT bucket BEFORE the httpx call, that the
buckets are independent, that the Liberator cap is placement-only (cancel /
heartbeat / reconciler fetches stay unthrottled), and that a throttle never
raises to the caller. Determinism: a ``SpyBucket`` records acquires into a shared
event log; a deterministic fake clock + recording sleep make the wait assertable;
``respx`` records the wire send so acquire-before-send is provable by order.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import httpx
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
from src.quant_execution_engine.adapters.liberator.transport import LiberatorTransport
from src.quant_execution_engine.adapters.rate_limit import TokenBucket
from src.quant_execution_engine.adapters.settrade.adapter import SettradeAdapter
from src.quant_execution_engine.adapters.settrade.client import SettradeClient
from src.quant_execution_engine.contracts.enums import Market

from tests.conftest import make_order

# ---- shared fake clock ------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class _SpyBucket(TokenBucket):
    """A TokenBucket that records each acquire (by name) into a shared log."""

    def __init__(self, rate: float, *, name: str, log: list[str], clock: _Clock) -> None:
        super().__init__(rate, name=name, now=clock.now, sleep=clock.sleep)
        self._log = log

    async def acquire(self) -> None:
        self._log.append(f"acquire:{self._name}")
        await super().acquire()


# ---- Settrade (D1) ----------------------------------------------------------

_SBASE = "https://open-api-test.settrade.com"
_SBROKER = "098"
_SCODE = "ABCAPP"
_SAPP_ID = "app-id-xyz"
_SAPP_SECRET = base64.b64encode((0x1234567890ABCDEF).to_bytes(32, "big")).decode()
_SPIN = "987654"
_SACCOUNT = "ACC-TEST"
_STOKEN = {
    "token_type": "Bearer",
    "access_token": "atk",
    "refresh_token": "rtk",
    "expires_in": 1800,
}


def _settrade_login_url() -> str:
    return f"{_SBASE}/api/oam/v1/{_SBROKER}/broker-apps/{_SCODE}/login"


def _set_orders_url() -> str:
    return f"{_SBASE}/api/seos/v3/{_SBROKER}/accounts/{_SACCOUNT}/orders"


def _spy_settrade_client(
    log: list[str], clock: _Clock, *, get_rate: float = 1.0, write_rate: float = 1.0
) -> SettradeClient:
    """A real client with its GET/WRITE buckets swapped for spies (white-box)."""
    client = SettradeClient(
        base_url=_SBASE,
        app_id=SecretStr(_SAPP_ID),
        app_secret=SecretStr(_SAPP_SECRET),
        app_code=_SCODE,
        broker_id=_SBROKER,
    )
    client._get_limiter = _SpyBucket(get_rate, name="settrade_get", log=log, clock=clock)
    client._write_limiter = _SpyBucket(write_rate, name="settrade_write", log=log, clock=clock)
    return client


def _settrade_order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "settrade", "price": "100.00"}
    payload.update(overrides)
    return make_order(**payload)


def _record_send(log: list[str], label: str) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(_request: httpx.Request, **_kw: Any) -> httpx.Response:
        log.append(f"send:{label}")
        return httpx.Response(200, json={"orderNo": "SET-1"})

    return _handler


@respx.mock
async def test_settrade_write_acquires_write_bucket_before_send() -> None:
    log: list[str] = []
    clock = _Clock()
    respx.post(_settrade_login_url()).respond(json=_STOKEN)
    respx.post(_set_orders_url()).mock(side_effect=_record_send(log, "place"))
    client = _spy_settrade_client(log, clock)
    adapter = SettradeAdapter(
        clients={Market.SET: client, Market.TFEX: client},
        broker_id=_SBROKER,
        pin=SecretStr(_SPIN),
    )
    await adapter.place(_settrade_order())
    # The WRITE bucket was acquired, and BEFORE the wire send.
    assert "acquire:settrade_write" in log
    assert log.index("acquire:settrade_write") < log.index("send:place")
    # The GET bucket was NOT touched by a pure write.
    assert "acquire:settrade_get" not in log
    await adapter.aclose()


@respx.mock
async def test_settrade_get_acquires_get_bucket_before_send() -> None:
    log: list[str] = []
    clock = _Clock()
    respx.post(_settrade_login_url()).respond(json=_STOKEN)

    def _orders(_request: httpx.Request, **_kw: Any) -> httpx.Response:
        log.append("send:get_orders")
        return httpx.Response(200, json=[])

    respx.get(_set_orders_url()).mock(side_effect=_orders)
    client = _spy_settrade_client(log, clock)
    # A GET via the client read path (the adapter's open-orders read).
    await client.get_json(f"api/seos/v3/{_SBROKER}/accounts/{_SACCOUNT}/orders")
    assert "acquire:settrade_get" in log
    assert log.index("acquire:settrade_get") < log.index("send:get_orders")
    assert "acquire:settrade_write" not in log
    await client.aclose()


@respx.mock
async def test_settrade_get_and_write_buckets_are_independent() -> None:
    """Rate 1/s each: a GET then a WRITE both run immediately (separate pools)."""
    log: list[str] = []
    clock = _Clock()
    respx.post(_settrade_login_url()).respond(json=_STOKEN)
    respx.get(_set_orders_url()).respond(json=[])
    respx.post(_set_orders_url()).respond(json={"orderNo": "SET-1"})
    client = _spy_settrade_client(log, clock, get_rate=1.0, write_rate=1.0)
    await client.get_json(f"api/seos/v3/{_SBROKER}/accounts/{_SACCOUNT}/orders")
    await client.post_json(f"api/seos/v3/{_SBROKER}/accounts/{_SACCOUNT}/orders", {"symbol": "PTT"})
    # Neither waited — the GET drained the GET bucket only, WRITE stayed full.
    assert clock.sleeps == []
    await client.aclose()


@respx.mock
async def test_settrade_rapid_writes_serialise_without_raising() -> None:
    """Two rapid WRITEs at 1/s: the second awaits ≈1s, neither drops nor raises."""
    log: list[str] = []
    clock = _Clock()
    respx.post(_settrade_login_url()).respond(json=_STOKEN)
    respx.post(_set_orders_url()).respond(json={"orderNo": "SET-1"})
    client = _spy_settrade_client(log, clock, write_rate=1.0)
    path = f"api/seos/v3/{_SBROKER}/accounts/{_SACCOUNT}/orders"
    await client.post_json(path, {"symbol": "PTT"})  # free (full)
    await client.post_json(path, {"symbol": "PTT"})  # waits ≈1/rate
    assert clock.sleeps  # a wait was enforced
    assert clock.sleeps[0] == 1.0
    await client.aclose()


@respx.mock
async def test_settrade_disabled_bucket_never_waits() -> None:
    """rate=0 ⇒ unlimited: many rapid writes, zero waits (no deadlock)."""
    log: list[str] = []
    clock = _Clock()
    respx.post(_settrade_login_url()).respond(json=_STOKEN)
    respx.post(_set_orders_url()).respond(json={"orderNo": "SET-1"})
    client = _spy_settrade_client(log, clock, write_rate=0.0)
    path = f"api/seos/v3/{_SBROKER}/accounts/{_SACCOUNT}/orders"
    for _ in range(20):
        await client.post_json(path, {"symbol": "PTT"})
    assert clock.sleeps == []
    await client.aclose()


# ---- Liberator (D2) ---------------------------------------------------------

_LBASE = "http://liberator-trading-api:8200/api/v1"


def _ok_place(order_no: str = "3064") -> dict[str, Any]:
    return {
        "success": True,
        "message": "placed",
        "data": {"errorCode": 0, "errMsg": "", "result": {"orderNo": order_no}},
    }


def _spy_liberator_adapter(
    log: list[str], clock: _Clock, *, post_rate: float = 1.0
) -> LiberatorAdapter:
    transport = LiberatorTransport(base_url=_LBASE, api_key=SecretStr("test-key"))
    adapter = LiberatorAdapter(transport=transport, pin=SecretStr("987654"))
    adapter._place_limiter = _SpyBucket(post_rate, name="liberator_post", log=log, clock=clock)
    return adapter


def _liberator_order(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"broker": "liberator", "price": "123.45"}
    payload.update(overrides)
    return make_order(**payload)


@respx.mock
async def test_liberator_place_acquires_post_bucket_before_send() -> None:
    log: list[str] = []
    clock = _Clock()

    def _place(_request: httpx.Request, **_kw: Any) -> httpx.Response:
        log.append("send:place")
        return httpx.Response(200, json=_ok_place())

    respx.post(f"{_LBASE}/order/place/set").mock(side_effect=_place)
    adapter = _spy_liberator_adapter(log, clock)
    ack = await adapter.place(_liberator_order())
    assert not ack.rejected
    assert "acquire:liberator_post" in log
    assert log.index("acquire:liberator_post") < log.index("send:place")
    await adapter.aclose()


@respx.mock
async def test_liberator_rapid_places_serialise_without_raising() -> None:
    log: list[str] = []
    clock = _Clock()
    respx.post(f"{_LBASE}/order/place/set").respond(json=_ok_place())
    adapter = _spy_liberator_adapter(log, clock, post_rate=1.0)
    await adapter.place(_liberator_order())  # free (full)
    await adapter.place(_liberator_order())  # waits ≈1s
    assert clock.sleeps == [1.0]
    await adapter.aclose()


@respx.mock
async def test_liberator_cancel_is_not_throttled() -> None:
    """The placement cap does NOT cover cancel() — it must never queue a cancel."""
    log: list[str] = []
    clock = _Clock()
    respx.post(f"{_LBASE}/order/place/set").respond(json=_ok_place("3064"))
    respx.post(f"{_LBASE}/order/cancelled/set").respond(json={"success": True, "data": {}})
    adapter = _spy_liberator_adapter(log, clock, post_rate=1.0)
    order = _liberator_order()
    await adapter.place(order)  # caches the orderNo; consumes the one token
    log.clear()
    await adapter.cancel(order.client_order_id)  # must NOT acquire the place bucket
    assert "acquire:liberator_post" not in log
    assert clock.sleeps == []
    await adapter.aclose()


@respx.mock
async def test_liberator_heartbeat_and_reads_are_not_throttled() -> None:
    log: list[str] = []
    clock = _Clock()
    respx.get(f"{_LBASE}/order/health/set").respond(
        json={"status": "healthy", "auth_token_available": True}
    )
    adapter = _spy_liberator_adapter(log, clock, post_rate=1.0)
    assert await adapter.heartbeat() is True
    assert "acquire:liberator_post" not in log
    assert clock.sleeps == []
    await adapter.aclose()


@respx.mock
async def test_liberator_mapping_reject_consumes_no_token() -> None:
    """A mapping-rejected order never reaches the wire → no bucket acquire."""
    log: list[str] = []
    clock = _Clock()
    adapter = _spy_liberator_adapter(log, clock)
    # A SET price with >2 decimal places fails the Liberator wire mapping (never
    # silently re-quantized) — this happens BEFORE the placement cap acquire.
    bad = _liberator_order(price="123.456")
    ack = await adapter.place(bad)
    assert ack.rejected
    assert "acquire:liberator_post" not in log
    await adapter.aclose()

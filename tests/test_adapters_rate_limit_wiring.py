"""D2 wiring: the Liberator place bucket.

Proves the adapter acquires the CORRECT bucket BEFORE the httpx call, that the
Liberator cap is placement-only (cancel / heartbeat / reconciler fetches stay
unthrottled), and that a throttle never raises to the caller. Determinism: a
``SpyBucket`` records acquires into a shared event log; a deterministic fake clock
+ recording sleep make the wait assertable; ``respx`` records the wire send so
acquire-before-send is provable by order.
"""

from __future__ import annotations

from typing import Any

import httpx
import respx
from pydantic import SecretStr
from src.quant_execution_engine.adapters.liberator.adapter import LiberatorAdapter
from src.quant_execution_engine.adapters.liberator.transport import LiberatorTransport
from src.quant_execution_engine.adapters.rate_limit import TokenBucket

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

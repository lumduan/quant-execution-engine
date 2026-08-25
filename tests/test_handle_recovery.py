"""TK-0423 / TK-0424: post-placement handle recovery and the caller-visible discriminator.

Two properties are load-bearing here and each has a test that goes red without it:

1. **An ack with no handle must NOT raise.** It surfaced as HTTP 500 for an order the
   venue had ACCEPTED — the caller could not tell "it failed" from "it is live and I
   lost the handle" ([[TK-0424]]).
2. **``PENDING`` and ``UNKNOWN`` must not be reachable from the same condition.** Only
   ``UNKNOWN`` means the venue was never read, and a caller that retries on it
   double-fills. A test that only checked "not CONFIRMED" would pass with the two
   collapsed, which is the whole failure mode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from src.quant_execution_engine.adapters.base import (
    AccountInfo,
    AmendAck,
    BrokerAdapter,
    CancelAck,
    PlaceAck,
    Position,
)
from src.quant_execution_engine.contracts.capabilities import CapabilitySet, lookup
from src.quant_execution_engine.contracts.enums import (
    Broker,
    HandleResolution,
    Market,
    OrderState,
)
from src.quant_execution_engine.contracts.orders import NormalizedOrder
from src.quant_execution_engine.core.handle_recovery import recover_handle
from src.quant_execution_engine.core.router import OrderRouter

from tests._fakes import FakeRedis, MemStore, patch_repositories
from tests.conftest import make_order, make_settings

_T0 = datetime(2026, 8, 25, 5, 4, 58, tzinfo=UTC)


async def _no_sleep(_seconds: float) -> None:
    """Collapse the cadence so tests measure LOGIC, not wall-clock."""
    return None


def _clock(*offsets_ms: float) -> Any:
    """A ``now()`` that walks the given offsets from ``_T0``, then holds the last."""
    seq = list(offsets_ms)

    def now() -> datetime:
        ms = seq.pop(0) if len(seq) > 1 else seq[0]
        return _T0 + timedelta(milliseconds=ms)

    return now


# --------------------------------------------------------------- the burst itself


async def test_confirmed_on_the_first_attempt_does_not_poll_again() -> None:
    """The venue answers at ~650 ms and our POST returns at ~1070 ms, so the common
    case is resolved before the burst ever sleeps. Asserting the call COUNT is the
    point: a burst that always spends its budget would load a shared venue session
    for nothing."""
    calls: list[str] = []

    async def resolve(cid: str) -> bool:
        calls.append(cid)
        return True

    result = await recover_handle(
        resolve,
        client_order_id="cid-1",
        submitted_at=_T0,
        cadence_seconds=0.25,
        deadline_seconds=1.5,
        now=_clock(1070),
        sleep=_no_sleep,
    )
    assert result is HandleResolution.CONFIRMED
    assert calls == ["cid-1"]


async def test_venue_read_but_not_there_yet_is_PENDING() -> None:
    """Read fine, order not resolvable yet -> PENDING. It is working; do not resubmit."""

    async def resolve(_cid: str) -> bool:
        return False

    result = await recover_handle(
        resolve,
        client_order_id="cid-2",
        submitted_at=_T0,
        cadence_seconds=0.25,
        deadline_seconds=1.5,
        now=_clock(1000, 1250, 1500),
        sleep=_no_sleep,
    )
    assert result is HandleResolution.PENDING


async def test_venue_unreadable_is_UNKNOWN_not_PENDING() -> None:
    """Every read FAILED -> UNKNOWN. The order may be live with no handle recovered."""

    async def resolve(_cid: str) -> bool:
        raise RuntimeError("bridge unreachable")

    result = await recover_handle(
        resolve,
        client_order_id="cid-3",
        submitted_at=_T0,
        cadence_seconds=0.25,
        deadline_seconds=1.5,
        now=_clock(1000, 1250, 1500),
        sleep=_no_sleep,
    )
    assert result is HandleResolution.UNKNOWN


async def test_PENDING_and_UNKNOWN_are_NOT_the_same_value() -> None:
    """🔴 THE DISCRIMINATOR TEST — the two must differ, from the same call shape.

    Both branches below are "we did not get the handle". Asserting only
    ``is not CONFIRMED`` would pass with them collapsed into one value, and a caller
    would then be unable to tell "the venue says it is working" from "we never
    reached the venue" — the reading that costs money, because a retry on the second
    double-fills. So this asserts they are DIFFERENT, not merely that each is wrong.
    """

    async def read_ok_not_found(_cid: str) -> bool:
        return False

    async def read_failed(_cid: str) -> bool:
        raise RuntimeError("bridge unreachable")

    pending = await recover_handle(
        read_ok_not_found,
        client_order_id="cid-4",
        submitted_at=_T0,
        cadence_seconds=0.25,
        deadline_seconds=1.5,
        now=_clock(1000, 1500),
        sleep=_no_sleep,
    )
    unknown = await recover_handle(
        read_failed,
        client_order_id="cid-4",
        submitted_at=_T0,
        cadence_seconds=0.25,
        deadline_seconds=1.5,
        now=_clock(1000, 1500),
        sleep=_no_sleep,
    )

    # A set of both, rather than `pending is not unknown`: identity comparison of two
    # enum members is narrowed away statically, so the runtime distinctness would stop
    # being checked at all. This asserts the two values AND that they are two.
    assert {pending, unknown} == {HandleResolution.PENDING, HandleResolution.UNKNOWN}


async def test_a_read_that_succeeds_ONCE_downgrades_to_PENDING_not_UNKNOWN() -> None:
    """One successful read among failures still proves the venue was reachable.

    UNKNOWN claims "we never got an answer from the venue"; if any attempt returned
    cleanly, that claim is false and PENDING is the honest report.
    """
    outcomes: list[Exception | bool] = [RuntimeError("down"), False]

    async def resolve(_cid: str) -> bool:
        nxt = outcomes.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    result = await recover_handle(
        resolve,
        client_order_id="cid-5",
        submitted_at=_T0,
        cadence_seconds=0.25,
        deadline_seconds=1.5,
        now=_clock(1000, 1400, 1500),
        sleep=_no_sleep,
    )
    assert result is HandleResolution.PENDING


async def test_budget_is_anchored_on_SUBMIT_not_on_entry() -> None:
    """The venue answers BEFORE our POST returns, so an ack-anchored clock starts late.

    Here the placement round-trip has already eaten the whole budget. An entry-anchored
    burst would still poll for a further 1.5 s; a submit-anchored one stops — but must
    STILL make one attempt, because that is exactly when the venue has had longest to
    answer.
    """
    calls: list[str] = []

    async def resolve(cid: str) -> bool:
        calls.append(cid)
        return False

    result = await recover_handle(
        resolve,
        client_order_id="cid-6",
        submitted_at=_T0,
        cadence_seconds=0.25,
        deadline_seconds=1.5,
        now=_clock(9000),  # 9 s after submit — long past the deadline
        sleep=_no_sleep,
    )
    assert len(calls) == 1, "exactly one attempt: never zero, never a spin"
    assert result is HandleResolution.PENDING


# --------------------------------------------------- the router integration (TK-0424)


class _NoHandleAdapter(BrokerAdapter):
    """A venue that ACCEPTS the order and returns no handle — Liberator's real behaviour."""

    def __init__(self, broker: Broker = Broker.LIBERATOR) -> None:
        super().__init__()
        self.broker = broker  # type: ignore[misc]
        self.place_calls: list[NormalizedOrder] = []

    async def place(self, order: NormalizedOrder) -> PlaceAck:
        self.place_calls.append(order)
        return PlaceAck(broker_order_id=None)  # accepted; no orderNo, as measured

    async def cancel(self, client_order_id: str) -> CancelAck:
        return CancelAck(ok=True)

    async def amend(
        self,
        client_order_id: str,
        new_price: Decimal | None = None,
        new_qty: int | None = None,
    ) -> AmendAck:
        return AmendAck(ok=True, semantics="cancel_replace")

    async def get_open_orders(self, account: str) -> list[NormalizedOrder]:
        return []

    async def get_positions(self, account: str) -> list[Position]:
        return []

    async def get_account(self, account: str) -> AccountInfo:
        return AccountInfo(account=account, buying_power=Decimal("1000000000"))

    def capabilities(self) -> tuple[CapabilitySet, ...]:
        return (lookup(self.broker, Market.SET),)

    async def heartbeat(self) -> bool:
        return True


_ACCOUNT = "70173292"


def _micro_live_router(
    monkeypatch: pytest.MonkeyPatch,
    *,
    handle_resolver: Any | None = None,
) -> tuple[OrderRouter, MemStore, _NoHandleAdapter]:
    """A router at micro_live wired to a venue that returns no handle.

    micro_live is constructed DIRECTLY rather than soaked into: a defect that can only
    fire at micro_live is one that testing at sim cannot reach.
    """
    store = MemStore()
    patch_repositories(monkeypatch, store)
    adapter = _NoHandleAdapter()
    settings = make_settings(
        stage="micro_live",
        real_routing_accounts=[_ACCOUNT],
        submit_lock_wait_ms=120,
        handle_recovery_cadence_ms=1,
        handle_recovery_deadline_ms=5,
    )
    router = OrderRouter(
        settings=settings,
        pool=object(),
        redis=FakeRedis(),
        liberator_adapter=adapter,
        handle_resolver=handle_resolver,
    )
    return router, store, adapter


async def test_ack_without_handle_NO_LONGER_RAISES(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 TK-0424 regression. This exact path returned HTTP 500 for a LIVE order.

    No resolver is configured, which is the weakest case — it must still not raise.
    """
    router, store, adapter = _micro_live_router(monkeypatch)
    order = make_order(broker="liberator", account=_ACCOUNT)

    outcome = await router.submit(order)  # must not raise

    assert adapter.place_calls, "the order really was routed to the venue"
    assert outcome.resolution is HandleResolution.UNKNOWN
    assert outcome.result.broker_order_id is None
    # The row is left honestly un-acked rather than given an invented handle;
    # the steady reconcile loop repairs it.
    assert store.orders[order.client_order_id]["status"] is OrderState.PENDING_NEW


async def test_resolver_recovers_the_handle_and_reports_CONFIRMED(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a resolver that finds the order, the caller gets the handle on the POST."""
    seen: list[str] = []

    async def resolver(cid: str) -> bool:
        seen.append(cid)
        return True

    router, store, _ = _micro_live_router(monkeypatch, handle_resolver=resolver)
    order = make_order(broker="liberator", account=_ACCOUNT)

    outcome = await router.submit(order)

    assert seen == [order.client_order_id]
    assert outcome.resolution is HandleResolution.CONFIRMED


async def test_unreadable_venue_reports_UNKNOWN_and_still_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver that cannot reach the venue degrades the REPORT, never the response."""

    async def resolver(_cid: str) -> bool:
        raise RuntimeError("bridge unreachable")

    router, _store, _ = _micro_live_router(monkeypatch, handle_resolver=resolver)
    order = make_order(broker="liberator", account=_ACCOUNT)

    outcome = await router.submit(order)

    assert outcome.resolution is HandleResolution.UNKNOWN


async def test_a_normal_ack_is_untouched_by_any_of_this(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: an ack that DOES carry a handle never enters recovery.

    Without this, every assertion above is satisfiable by a router that simply
    reports UNKNOWN for everything.
    """
    called = False

    async def resolver(_cid: str) -> bool:
        nonlocal called
        called = True
        return True

    store = MemStore()
    patch_repositories(monkeypatch, store)
    router = OrderRouter(
        settings=make_settings(submit_lock_wait_ms=120),
        pool=object(),
        redis=FakeRedis(),
        handle_resolver=resolver,
    )
    outcome = await router.submit(make_order())  # sim: always issues its own handle

    assert not called, "recovery must not run when the ack already carried a handle"
    assert outcome.resolution is HandleResolution.CONFIRMED
    assert outcome.result.broker_order_id is not None

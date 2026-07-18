"""TokenBucket: deterministic refill, await-on-deficit, single WARN, disabled no-op.

Pure-asyncio limiter (Phase 6 / D). A fake monotonic clock + a recording sleep
make the wait maths assertable without real time passing. The recording sleep
advances the fake clock by the slept duration (a well-behaved scheduler), so
back-to-back acquires see the bucket regenerate exactly as in production.
"""

from __future__ import annotations

import logging

import pytest
from src.quant_execution_engine.adapters.rate_limit import TokenBucket


class _Clock:
    """A monotonic fake clock advanced explicitly or by the recording sleep."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds  # a well-behaved scheduler advances wall-clock time


def _bucket(rate: float, clock: _Clock, *, name: str = "test") -> TokenBucket:
    return TokenBucket(rate, name=name, now=clock.now, sleep=clock.sleep)


async def test_first_acquire_is_immediate() -> None:
    clock = _Clock()
    bucket = _bucket(1.0, clock)
    await bucket.acquire()
    assert clock.sleeps == []  # started full, no wait


async def test_second_rapid_acquire_waits_one_over_rate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    bucket = _bucket(2.0, clock)  # capacity 2 → first two are free
    await bucket.acquire()
    await bucket.acquire()
    with caplog.at_level(logging.WARNING):
        await bucket.acquire()  # third overflows the burst → waits 1/rate = 0.5s
    assert clock.sleeps == pytest.approx([0.5])
    waits = [r for r in caplog.records if "rate_limit_wait" in r.message]
    assert len(waits) == 1  # exactly ONE warn per wait
    assert waits[0].name.endswith("rate_limit")  # logged by the rate_limit module


async def test_warn_line_carries_bucket_name(caplog: pytest.LogCaptureFixture) -> None:
    clock = _Clock()
    bucket = _bucket(1.0, clock, name="liberator_place")
    await bucket.acquire()  # drains the single token
    with caplog.at_level(logging.WARNING):
        await bucket.acquire()
    waits = [r for r in caplog.records if "rate_limit_wait" in r.message]
    assert len(waits) == 1
    assert waits[0].getMessage().startswith("liberator_place_rate_limit_wait")


async def test_rate_one_serialises_each_acquire() -> None:
    clock = _Clock()
    bucket = _bucket(1.0, clock)  # capacity 1
    await bucket.acquire()  # free (full)
    await bucket.acquire()  # waits 1.0s
    await bucket.acquire()  # waits 1.0s again
    assert clock.sleeps == pytest.approx([1.0, 1.0])


async def test_slow_caller_never_waits() -> None:
    clock = _Clock()
    bucket = _bucket(10.0, clock)
    await bucket.acquire()  # full → free
    clock.t += 1.0  # a full second elapses: tokens fully regenerate
    await bucket.acquire()
    await bucket.acquire()
    assert clock.sleeps == []  # regenerated faster than consumed → no wait


async def test_independent_buckets_do_not_interfere() -> None:
    clock = _Clock()
    get_bucket = _bucket(1.0, clock, name="streaming_pro_post")
    write_bucket = _bucket(1.0, clock, name="liberator_place")
    await get_bucket.acquire()  # drains GET only
    await write_bucket.acquire()  # WRITE is still full → immediate
    assert clock.sleeps == []  # neither waited; separate token pools


async def test_rate_zero_is_a_noop_and_never_deadlocks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    bucket = _bucket(0.0, clock)
    assert bucket.enabled is False
    with caplog.at_level(logging.WARNING):
        for _ in range(100):
            await bucket.acquire()  # unlimited: must never wait or warn
    assert clock.sleeps == []
    assert [r for r in caplog.records if "rate_limit_wait" in r.message] == []


async def test_negative_rate_is_also_disabled() -> None:
    clock = _Clock()
    bucket = _bucket(-5.0, clock)
    assert bucket.enabled is False
    await bucket.acquire()
    assert clock.sleeps == []


async def test_partial_regeneration_shortens_the_wait() -> None:
    clock = _Clock()
    bucket = _bucket(2.0, clock)  # capacity 2
    await bucket.acquire()
    await bucket.acquire()  # bucket now empty
    clock.t += 0.25  # 0.25s → 0.5 token regenerated
    await bucket.acquire()  # needs 0.5 more token → wait = 0.5/2 = 0.25s
    assert clock.sleeps == pytest.approx([0.25])


async def test_acquire_consumes_exactly_one_after_wait() -> None:
    clock = _Clock()
    bucket = _bucket(1.0, clock)
    await bucket.acquire()  # full → free
    await bucket.acquire()  # waited 1.0s, consumed the regenerated token
    # Immediately after, the bucket is empty again → a third acquire waits afresh.
    await bucket.acquire()
    assert clock.sleeps == pytest.approx([1.0, 1.0])

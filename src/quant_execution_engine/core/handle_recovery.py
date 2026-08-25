"""Post-placement handle recovery — the bounded burst behind submit-to-known (TK-0423).

WHY THIS EXISTS
---------------
The Liberator place-ack carries **no** ``orderNo`` — measured 2026-08-25 across both
order classes, so the omission is unconditional, not a terminal-on-arrival quirk:

  * 9 FOKs that died on arrival (``session:cash-carry``)  -> no handle
  * 1 DAY LIMIT that RESTED at the venue (this session)   -> no handle

The handle exists only in ``GET orders/{account}``. Before this module the router
raised ``AdapterError`` on the missing handle, so an order the venue had **accepted**
came back to the caller as HTTP 500 ([[TK-0424]]); the steady 12 s reconcile loop
repaired the durable row ~15 s later, long after the caller had already decided.

THE TWO NUMBERS THAT SHAPE THE DESIGN (measured, not assumed)
------------------------------------------------------------
  venue holds the terminal answer   567-752 ms after submit
  our own placement round-trip      959-1175 ms  (1068 ms here)

⇒ **the venue answers BEFORE our POST returns.** So the budget is anchored on the
persisted submit timestamp, never on the ack: an ack-anchored clock starts ~400 ms
late and no cadence recovers that. It also means the first attempt usually succeeds —
the cadence exists for the tail, not the common case.

Polling faster than the read itself (~200-244 ms) cannot produce a fresher answer; it
only queues reads and adds load to a venue session **shared with the capture plane**,
whose data is not backfillable. Hence a cadence at the read latency and a hard deadline
rather than a spin.

WHAT THIS MODULE IS NOT
-----------------------
It does not wait for a TERMINAL state. A resting order has none — waiting for one would
hang the POST until the close. It stops as soon as the venue has been *resolved* for this
order (handle recorded, or a terminal state applied); resting-vs-terminal is then a fact
reported, never a thing waited on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from src.quant_execution_engine.contracts.enums import HandleResolution

logger = logging.getLogger(__name__)

# One venue read for ONE order. True  -> resolved (handle recorded / terminal applied).
#                                False -> read fine, this order not resolvable yet.
#                                raises -> the venue could NOT be read.
HandleResolver = Callable[[str], Awaitable[bool]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def recover_handle(
    resolve: HandleResolver,
    *,
    client_order_id: str,
    submitted_at: datetime,
    cadence_seconds: float,
    deadline_seconds: float,
    min_polls: int = 1,
    now: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> HandleResolution:
    """Burst against venue truth until this order resolves, or the budget runs out.

    Two bounds, and the ORDER between them is the whole fix ([[TK-0426]]):

    * ``min_polls`` — the **retry floor**. At least this many attempts run, counted from
      whenever recovery starts. It exists because ``deadline_seconds`` is anchored on the
      *submit* timestamp, so the placement round-trip sits INSIDE the budget: at the
      measured mean (1197 ms against 1500 ms) only ~300 ms survived — about one retry —
      and a slow placement could leave none. 🔴 That made ``UNKNOWN`` (*"the venue could
      not be read"*) reachable because **our own call was slow**, firing the expensive
      signal for a cause unrelated to what it detects.
    * ``deadline_seconds`` — the **submit-anchored ceiling**, which may only cut the burst
      short **after** the floor is satisfied. It bounds total submit-to-known: 3 × 250 ms
      on top of the worst observed placement (1175 ms) = 1925 ms, inside the operator's
      2 s bar.

    ⚠️ A pure ack-anchored budget — the obvious fix — decouples correctly but drops the
    ceiling (1175 + 1500 = 2675 ms, past the bar). Floor-then-ceiling keeps both.

    Returns, and the distinction is the whole point:

    * :attr:`HandleResolution.CONFIRMED` — a read resolved the order.
    * :attr:`HandleResolution.PENDING` — the venue WAS read, and did not have it yet.
      The order is working; the steady reconciler will finish the job.
    * :attr:`HandleResolution.UNKNOWN` — every attempt failed to READ the venue. The
      order may be live with an unrecovered handle. 🔴 Never resubmit on this.
    """
    read_ok = False
    attempts = 0
    while True:
        attempts += 1
        try:
            if await resolve(client_order_id):
                logger.info(
                    "handle_recovery: %s CONFIRMED from venue truth in %d attempt(s), "
                    "%.0f ms after submit",
                    client_order_id,
                    attempts,
                    _elapsed_ms(now(), submitted_at),
                )
                return HandleResolution.CONFIRMED
            read_ok = True
        except Exception as exc:  # noqa: BLE001 - any read failure is "we do not know"
            # Deliberately broad: the caller-visible contract is *did we read the
            # venue*, and every way of failing to read means the same thing here.
            logger.warning("handle_recovery: %s venue read failed: %s", client_order_id, exc)

        # 🔴 FLOOR BEFORE CEILING. `attempts >= min_polls` is checked FIRST and is
        # ANDed, so a placement that already ate the submit-anchored budget cannot
        # reduce the retry count to zero — which is exactly how UNKNOWN became
        # reachable for a healthy order ([[TK-0426]]).
        elapsed = _elapsed_ms(now(), submitted_at) / 1000.0
        if attempts >= min_polls and elapsed + cadence_seconds > deadline_seconds:
            break
        await sleep(cadence_seconds)

    resolution = HandleResolution.PENDING if read_ok else HandleResolution.UNKNOWN
    logger.warning(
        "handle_recovery: %s -> %s after %d attempt(s), %.0f ms after submit "
        "(order may be LIVE at the venue; never resubmit)",
        client_order_id,
        resolution.value,
        attempts,
        _elapsed_ms(now(), submitted_at),
    )
    return resolution


def _elapsed_ms(now_ts: datetime, submitted_at: datetime) -> float:
    """Milliseconds since submit, clamped at 0 so clock skew cannot go negative."""
    return max(0.0, (now_ts - submitted_at).total_seconds() * 1000.0)

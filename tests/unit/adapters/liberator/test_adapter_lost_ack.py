"""place() must NEVER raise after the venue has accepted (the 2026-08-25 live defect)."""

from __future__ import annotations

import respx
from src.quant_execution_engine.adapters.errors import AdapterError

from tests.unit.adapters.liberator.test_adapter_place import _BASE, _liberator_order, make_adapter


def _ack_without_order_no() -> dict[str, object]:
    """A successful venue ack whose result carries no orderNo — observed live 2026-08-25.

    The bridge returned 200 and the venue numbered the order 10993; only the ack's
    `data.result.orderNo` was absent.
    """
    return {"success": True, "data": {"errorCode": 0, "errMsg": "", "result": {}}}


@respx.mock
async def test_a_missing_orderNo_does_NOT_raise_because_the_venue_ALREADY_ACCEPTED() -> None:
    """🔴 The regression. This used to raise AdapterError -> HTTP 500 with an empty body.

    A 500 on a path that can retry is a DUPLICATE-ORDER shape. The order is live; the only
    thing missing is our handle to it. Reporting "failed" for an order that landed is the
    more dangerous of the two errors, and it is the one that happened.
    """
    respx.post(f"{_BASE}/order/place/set").respond(json=_ack_without_order_no())
    adapter = make_adapter()
    ack = await adapter.place(_liberator_order())  # must not raise
    assert ack.rejected is False, "the venue accepted it — this is not a rejection"
    assert ack.broker_order_id is None, "handle unknown, and said so rather than invented"
    await adapter.aclose()


@respx.mock
async def test_cancel_distinguishes_LIVE_AT_VENUE_from_unknown_order() -> None:
    """The two must not read alike: one is resubmittable, the other must never be.

    Before this, both returned "no broker_order_id mapping for client_order_id" — which is
    the same class of defect as the ack that caused it: a message that cannot distinguish
    two situations with opposite correct responses.
    """
    respx.post(f"{_BASE}/order/place/set").respond(json=_ack_without_order_no())
    adapter = make_adapter()
    order = _liberator_order()
    await adapter.place(order)

    live = await adapter.cancel(order.client_order_id)
    assert live.ok is False
    assert live.reason is not None
    assert "LIVE at the venue" in live.reason and "do NOT resubmit" in live.reason

    unknown = await adapter.cancel("a-cid-that-was-never-placed")
    assert unknown.ok is False and unknown.reason is not None
    assert "no broker_order_id mapping" in unknown.reason
    assert unknown.reason != live.reason, "the two situations must not report identically"
    await adapter.aclose()


@respx.mock
async def test_a_normal_ack_still_caches_the_handle_and_clears_the_marker() -> None:
    """Positive control. Without it, "does not raise" would be satisfied by an adapter that
    never resolves a handle at all — a different defect, not a fix."""
    respx.post(f"{_BASE}/order/place/set").respond(
        json={
            "success": True,
            "data": {"errorCode": 0, "errMsg": "", "result": {"orderNo": "10993"}},
        }
    )
    adapter = make_adapter()
    order = _liberator_order()
    ack = await adapter.place(order)
    assert ack.broker_order_id == "10993"
    assert order.client_order_id not in adapter._awaiting_order_no
    await adapter.aclose()


def test_place_contains_no_RAISE_STATEMENT_after_the_venue_write() -> None:
    """Structural guard: parse the AST, do not grep the source.

    ⚠️ The first version of this test grepped for the string "raise AdapterError" and failed
    against the *comment* that quotes the old code — matching prose, not a statement. A check
    that cannot tell a comment from an executable line is not a guard; `ast` can.
    """
    import ast
    import inspect
    import textwrap

    from src.quant_execution_engine.adapters.liberator import adapter as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod.LiberatorAdapter.place)))
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)]
    assert raises == [], f"place() must not raise after a venue write; found {len(raises)}"
    assert AdapterError is not None  # the type stays available for other failure paths

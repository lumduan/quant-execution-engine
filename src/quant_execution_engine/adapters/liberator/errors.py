"""Liberator-adapter errors (module-local, shared base per convention)."""

from __future__ import annotations

from typing import ClassVar

from src.quant_execution_engine.adapters.errors import AdapterError
from src.quant_execution_engine.contracts.errors import OrderRejectedError


class LiberatorAdapterError(AdapterError):
    """Base for every Liberator-adapter failure."""


class LiberatorTransportError(LiberatorAdapterError):
    """Connectivity/timeout/5xx/non-JSON failure reaching liberator-trading-api.

    These are the failures that feed the session circuit breaker (§G); a
    structured venue rejection is NOT a transport error — it travels as a
    rejected ack with the venue's reason.
    """


class LiberatorMappingError(LiberatorAdapterError):
    """The order cannot be expressed on the Liberator wire (pre-flight).

    Raised before any HTTP I/O; the adapter converts it into a rejected ack so
    the reason persists durably — never a silent drop.
    """


class LiberatorAccountNotFound(OrderRejectedError):
    """The account is not on the logged-in profile, or the venue refused it.

    Routes through the shared typed-error envelope like ``order_book_unavailable``;
    the ``code`` maps to ``404`` in ``error_handlers``.

    ⚠️ Raised rather than degrading to a zero balance **on purpose** ([[TK-0396]]):
    a ``0`` returned for an unknown account is indistinguishable from a real zero,
    and that ambiguity is the whole defect this replaces.
    """

    code: ClassVar[str] = "liberator_account_not_found"


class LiberatorPositionsUncaptured(OrderRejectedError):
    """Liberator positions cannot be read — the response schema is unknown.

    ➡️ **NO LONGER RAISED ANYWHERE, as of 2026-08-28 — and that is deliberate, not an
    orphan.** The operator opened real SET and TFEX positions, the element schema was
    captured (umbrella ``docs/reference/liberator-account-reads.md`` §2.2), and
    ``LiberatorAdapter.get_positions`` now parses it. This class and its **501** mapping
    in ``api/error_handlers.py`` are retained on purpose: the 501 status is still the
    right answer for *"the adapter cannot do this"* — ``StreamingProAdapter.get_positions``
    is still SET-only and has no equivalent refusal — and the docstring below records why
    a loud refusal beat an invented parse for the four months it stood. **Do not delete it
    on the grounds that nothing raises it; that is the point of this note.**

    The refusal was vindicated, incidentally: the ten field names it was declining to
    guess from turned out to be a **lower bound** (the venue sends 17 on TFEX) and one of
    them, ``optVal``, does not exist at all.

    --- the original reasoning, preserved ---

    ``POST /va/portfolio`` returns ``result.{list, stock}``, and **neither array has
    ever been observed non-empty** on this platform: no Liberator account holds
    anything, so the element shape (field names, types) has never been captured.

    Writing a parse against it would mean inventing field names — which is exactly
    how the defect in [[TK-0396]] was created: the previous implementation parsed
    ``data["positions"]``, a key the bridge never emits, and silently returned ``[]``
    for every account. **A loud refusal is the honest answer until a funded account
    holding a position is captured.** See ``docs/reference/liberator-account-reads.md``
    (umbrella) §7 for what would settle it.
    """

    code: ClassVar[str] = "liberator_positions_uncaptured"

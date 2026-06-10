"""Typed rejection taxonomy — the wire-visible error contract.

Every rejection the router can produce is a subclass of
:class:`OrderRejectedError` with a stable ``code`` that crosses the API as
``{"error": {"code", "message", "client_order_id?", "detail?"}}``. A duplicate
submit is deliberately NOT an error — it returns the prior result (ADR §A).
"""

from __future__ import annotations

from typing import Any, ClassVar

from src.quant_execution_engine.errors import ExecutionEngineError


class OrderRejectedError(ExecutionEngineError):
    """Base of every typed rejection; carries the stable wire ``code``."""

    code: ClassVar[str] = "rejected"

    def __init__(
        self,
        message: str,
        *,
        client_order_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.client_order_id = client_order_id
        self.detail = detail or {}


class PublicModeRejected(OrderRejectedError):
    """Order-submission/admin endpoint hit while the service is public/read-only."""

    code: ClassVar[str] = "public_mode"


class KillSwitchEngagedError(OrderRejectedError):
    """Global kill-switch engaged — all new submits rejected (hard rule 3)."""

    code: ClassVar[str] = "kill_switch_engaged"


class KillSwitchPinnedError(OrderRejectedError):
    """Runtime disengage refused: the env flag pins the switch on (env wins)."""

    code: ClassVar[str] = "kill_switch_env_pinned"


class StageRejected(OrderRejectedError):
    """The ``EXECUTION_ENGINE_STAGE`` ladder forbids the requested route."""

    code: ClassVar[str] = "stage_rejected"


class CapabilityError(OrderRejectedError):
    """Unsupported ``(broker, market, order_type, tif, position_effect)`` (D7)."""

    code: ClassVar[str] = "capability_unsupported"


class RiskRejected(OrderRejectedError):
    """PTRM pre-trade cap violated; ``detail['cap']`` names the cap."""

    code: ClassVar[str] = "risk_rejected"

    def __init__(
        self,
        message: str,
        *,
        cap: str,
        client_order_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        merged = {"cap": cap, **(detail or {})}
        super().__init__(message, client_order_id=client_order_id, detail=merged)
        self.cap = cap


class OrderNotFound(OrderRejectedError):
    """Unknown ``client_order_id``."""

    code: ClassVar[str] = "order_not_found"


class IllegalTransition(OrderRejectedError):
    """State change outside the frozen 13-edge graph (app guard or DB 23514)."""

    code: ClassVar[str] = "illegal_transition"


class ConcurrentSubmit(OrderRejectedError):
    """An identical submit is mid-flight and produced no durable row yet."""

    code: ClassVar[str] = "submit_in_flight"

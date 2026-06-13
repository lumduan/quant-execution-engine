"""Transport envelopes only — the domain contract lives in ``contracts/``."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.quant_execution_engine.contracts.capabilities import CapabilitySet
from src.quant_execution_engine.contracts.enums import Stage


class BrokerRuntimeHealth(BaseModel):
    """Per-broker runtime state (Phase 3 additive — absent when not configured)."""

    breaker_state: str  # "closed" | "open" | "half_open"
    session_healthy: bool | None = None  # last heartbeat result; None before the first
    # Phase 4.1 (additive, Settrade only): per-market last heartbeat — which app
    # is alive/dead under a multi-app broker ({"SET": bool|None, "TFEX": ...}).
    sessions: dict[str, bool | None] | None = None


class OrderBookHealth(BaseModel):
    """Order-book service runtime state (Phase 5 additive — None when off)."""

    active_provider: str  # the current failover-active provider
    providers: list[str]  # every configured provider
    cached_symbols: int  # (symbol, market) keys currently held
    subscribers: int  # live SSE subscriber queues across all keys


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "quant-execution-engine"
    version: str
    stage: Stage
    public_mode: bool
    brokers: dict[str, BrokerRuntimeHealth] | None = None
    order_book: OrderBookHealth | None = None


class CapabilitiesResponse(BaseModel):
    stage: Stage
    capabilities: tuple[CapabilitySet, ...]
    brokers: dict[str, BrokerRuntimeHealth] | None = None


class KillSwitchStateResponse(BaseModel):
    engaged: bool
    source: str | None = Field(default=None, description="'env' | 'redis' | null")


class KillSwitchEngageResponse(BaseModel):
    """``POST /admin/kill-switch/engage`` result.

    ``already_engaged`` makes the trip idempotent: a second engage returns 200
    with ``already_engaged=true`` and ``cancelled_count=0`` (no second sweep).
    ``cancelled_count`` mirrors ``len(cancelled)`` for callers that only want the
    number; the ``cancelled``/``failed`` cid lists stay (additive).
    """

    engaged: bool
    already_engaged: bool = False
    cancelled_count: int = 0
    cancelled: list[str]
    failed: list[str]


class AmendOrderRequest(BaseModel):
    """``PATCH /orders/{cid}`` body — amend price and/or quantity.

    ``new_client_order_id`` is supplied ONLY for cancel_replace brokers
    (Liberator); native brokers (Settrade) keep the same id and must omit it —
    that asymmetry is enforced in the router, by amend semantics. At least one
    of ``new_price``/``new_qty`` is required (422 at the boundary).
    """

    model_config = ConfigDict(extra="forbid")

    new_price: Decimal | None = None
    new_qty: int | None = Field(default=None, gt=0)
    new_client_order_id: str | None = None

    @field_validator("new_price", mode="before")
    @classmethod
    def _no_float_money(cls, value: object) -> object:
        """Money is Decimal-as-string on the wire — reject binary floats outright."""
        if isinstance(value, float):
            raise ValueError("new_price must be sent as a string, never a float")
        return value

    @field_validator("new_price")
    @classmethod
    def _positive_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("new_price must be > 0")
        return value

    @field_validator("new_client_order_id")
    @classmethod
    def _uuid4_when_present(cls, value: str | None) -> str | None:
        """ADR §A: the replacement id is a UUIDv4, format-validated here."""
        if value is None:
            return None
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("new_client_order_id must be a UUIDv4") from exc
        if parsed.version != 4:
            raise ValueError("new_client_order_id must be a UUIDv4")
        return value

    @model_validator(mode="after")
    def _require_a_change(self) -> AmendOrderRequest:
        if self.new_price is None and self.new_qty is None:
            raise ValueError("amend requires at least one of new_price or new_qty")
        return self

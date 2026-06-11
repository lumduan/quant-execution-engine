"""Transport envelopes only — the domain contract lives in ``contracts/``."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.quant_execution_engine.contracts.capabilities import CapabilitySet
from src.quant_execution_engine.contracts.enums import Stage


class BrokerRuntimeHealth(BaseModel):
    """Per-broker runtime state (Phase 3 additive — absent when not configured)."""

    breaker_state: str  # "closed" | "open" | "half_open"
    session_healthy: bool | None = None  # last heartbeat result; None before the first


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "quant-execution-engine"
    version: str
    stage: Stage
    public_mode: bool
    brokers: dict[str, BrokerRuntimeHealth] | None = None


class CapabilitiesResponse(BaseModel):
    stage: Stage
    capabilities: tuple[CapabilitySet, ...]
    brokers: dict[str, BrokerRuntimeHealth] | None = None


class KillSwitchStateResponse(BaseModel):
    engaged: bool
    source: str | None = Field(default=None, description="'env' | 'redis' | null")


class KillSwitchEngageResponse(BaseModel):
    engaged: bool
    cancelled: list[str]
    failed: list[str]

"""Transport envelopes only — the domain contract lives in ``contracts/``."""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.quant_execution_engine.contracts.capabilities import CapabilitySet
from src.quant_execution_engine.contracts.enums import Stage


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "quant-execution-engine"
    version: str
    stage: Stage
    public_mode: bool


class CapabilitiesResponse(BaseModel):
    stage: Stage
    capabilities: tuple[CapabilitySet, ...]


class KillSwitchStateResponse(BaseModel):
    engaged: bool
    source: str | None = Field(default=None, description="'env' | 'redis' | null")


class KillSwitchEngageResponse(BaseModel):
    engaged: bool
    cancelled: list[str]
    failed: list[str]

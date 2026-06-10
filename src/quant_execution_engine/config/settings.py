"""Central configuration loaded from environment and ``.env``.

Env prefix ``EXECUTION_ENGINE_`` (decision E1). Safety defaults (E2/E3): the
stage ladder defaults to ``sim``, public mode defaults to ``true`` (order
submission disabled), and the kill-switch env flag is the boot-time backstop.
Broker credentials never appear here in Phase 2 — no real adapter exists.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.quant_execution_engine.contracts.enums import Stage


class Settings(BaseSettings):
    """Service settings; see ``.env.example`` for the operator-facing template."""

    model_config = SettingsConfigDict(
        env_prefix="EXECUTION_ENGINE_",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # Safety posture (E2/E3 + hard rule 3)
    public_mode: bool = True
    stage: Stage = Stage.SIM
    kill_switch_engaged: bool = False

    # Backing stores
    pg_dsn: str = "postgresql://quant:quant@quant-postgres:5432/db_execution"
    redis_url: str = "redis://execution-redis:6379/0"
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 10

    # Optional shared key for X-API-Key (hmac-compared; warn when unset)
    api_key: str | None = None

    # PTRM caps (D11) — pre-trade risk gate
    risk_max_order_qty: int = 1000
    risk_max_order_value: Decimal = Decimal("1000000")
    risk_max_orders_per_second: int = 5
    risk_duplicate_burst_window_seconds: int = 2

    # Idempotent-submit single-flight lock
    submit_lock_ttl_seconds: int = 10
    submit_lock_wait_ms: int = 300

    # SimAdapter
    sim_default_fill_price: Decimal = Decimal("100")

    # Informational (compose maps host port -> container :8000)
    host_port: int = 8400


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()

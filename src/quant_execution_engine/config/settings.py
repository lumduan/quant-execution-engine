"""Central configuration loaded from environment and ``.env``.

Env prefix ``EXECUTION_ENGINE_`` (decision E1). Safety defaults (E2/E3): the
stage ladder defaults to ``sim``, public mode defaults to ``true`` (order
submission disabled), and the kill-switch env flag is the boot-time backstop.
Broker secrets (Liberator PIN / api-key, Phase 3) are ``SecretStr`` sourced
from the gitignored ``.env`` only — never committed, never logged.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
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

    # Broker: Liberator (Phase 3) — adapter target + secrets. Secrets are
    # SecretStr, presence-checked only at runtime creation (settings must still
    # load in sim, where no broker is configured); NEVER logged.
    liberator_base_url: str = "http://liberator-trading-api:8200/api/v1"
    liberator_api_key: SecretStr | None = None
    liberator_pin: SecretStr | None = None
    liberator_heartbeat_interval_seconds: int = 30
    liberator_circuit_breaker_threshold: int = 3
    liberator_reconcile_interval_seconds: int = 12

    # Broker: Settrade Open API v2 (Phase 4) — cloud venue + secrets. Same
    # discipline as Liberator: creds/PIN are SecretStr, all optional (settings
    # must load in sim, where Settrade is not configured), presence-checked only
    # at runtime creation; NEVER logged. No compose overlay — Settrade is a cloud
    # API; creds ride docker-compose.private.yml's env_file.
    settrade_base_url: str = "https://open-api.settrade.com"
    settrade_app_id: SecretStr | None = None
    settrade_app_secret: SecretStr | None = None
    settrade_app_code: str | None = None
    settrade_broker_id: str | None = None
    settrade_account_no: str | None = None
    settrade_pin: SecretStr | None = None
    # Per-market OAuth app overrides (Phase 4.1). The real broker InnovestX (023)
    # splits the two markets across two OAuth apps (ALGO_EQ = SET equity, ALGO =
    # TFEX derivatives, each with its own app_id/app_secret/app_code). A market's
    # trio (app_id + app_secret + app_code) is read INDEPENDENTLY: complete →
    # used; PARTIAL (1–2 fields) → that market is left UNCONFIGURED with a boot
    # WARNING naming the missing fields (fails loud — NEVER falls back to the
    # shared trio, so a forgotten secret can never route a market through the
    # wrong app); absent → falls back to the shared ``settrade_app_*`` trio
    # (the sandbox single-app path). ``broker_id``/``base_url``/``pin``/intervals
    # stay shared across both markets.
    settrade_equity_app_id: SecretStr | None = None
    settrade_equity_app_secret: SecretStr | None = None
    settrade_equity_app_code: str | None = None
    settrade_derivatives_app_id: SecretStr | None = None
    settrade_derivatives_app_secret: SecretStr | None = None
    settrade_derivatives_app_code: str | None = None
    settrade_heartbeat_interval_seconds: int = 30
    settrade_circuit_breaker_threshold: int = 3
    settrade_reconcile_interval_seconds: int = 12
    settrade_token_refresh_margin_seconds: int = 100

    # Order book service (Phase 5) — a normalized, read-only L2 cache fed by the
    # Settrade realtime + Liberator WebSocket feeds (ADR D17–D24). The whole
    # service defaults OFF (D24): nothing connects, no SDK imports, the engine's
    # existing behavior is bit-for-bit unchanged until an operator opts in. The
    # endpoints carry no order data/credential and are public-mode-readable.
    order_book_enabled: bool = False
    order_book_primary_provider: Literal["settrade", "liberator"] = "settrade"
    # JSON map symbol -> provider name, e.g. '{"AOT": "liberator"}'.
    order_book_symbol_overrides: dict[str, str] = Field(default_factory=dict)
    order_book_failover_error_threshold: int = 3
    order_book_failover_window_seconds: int = 30
    order_book_cache_max_age_seconds: int = 5
    order_book_cache_max_symbols: int = 500
    # Sim fill-price fallback hop 2 (last close) + order-update stream knobs. The
    # stream_* values are consumed by the streaming sub-steps (3E–3G); they land
    # here once so settings stay a single source of truth.
    market_data_base_url: str | None = None
    stream_keepalive_seconds: int = 15
    stream_ring_buffer_size: int = 1024
    stream_subscriber_queue_size: int = 256

    # Informational (compose maps host port -> container :8000)
    host_port: int = 8400


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()

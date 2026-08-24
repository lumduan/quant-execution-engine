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
    # Legacy Phase-2 burst window. Retained so older .env files still load, but
    # the Phase-6 unified duplicate-burst guard uses ``duplicate_burst_window_seconds``
    # below (Design Decision §1); this value is no longer read by the burst check.
    risk_duplicate_burst_window_seconds: int = 2

    # Per-account PTRM caps (Phase 6 / A1). JSON maps account -> cap. When an
    # account is present in a map its cap binds; an account absent from the map
    # falls back to the global ``risk_max_*`` cap above (never a silent skip).
    # Enforced in EVERY stage, including ``sim`` — the risk gate is not
    # mode-dependent. Notional is ``Decimal`` end-to-end.
    account_max_notional: dict[str, Decimal] = Field(default_factory=dict)
    account_max_qty: dict[str, int] = Field(default_factory=dict)

    # EH6 — the accounts THIS node is the authoritative real-router for.
    # 🔴 Empty means "route nothing real", NOT "route anything". Absent ⇒ refuse.
    # ⚠️ These are 8-digit TRADING accounts (`<login><suffix>`), not the 7-digit logins in the
    # umbrella CLAUDE.md broker table — one suffix apart, and NormalizedOrder.account carries
    # the former.
    real_routing_accounts: list[str] = Field(default_factory=list)

    # Price-band advisory pre-trade check (Phase 6 / A2). When enabled AND a
    # market-data base URL is configured, a LIMIT order whose price deviates from
    # the symbol's last close by more than ``price_band_max_pct`` percent is
    # rejected (422). MARKET orders bypass; a market-data fetch failure is
    # advisory (WARN + pass). Default OFF — enabling it is an operator choice.
    price_band_enabled: bool = False
    price_band_max_pct: Decimal = Decimal("10.0")

    # Unified duplicate-burst guard (Phase 6 / A3, Design Decision §1). Blocks a
    # second order carrying the same economic fingerprint
    # ``(account, symbol, side, quantity, order_type, price)`` under a DIFFERENT
    # client_order_id within the window (409). Same-cid resends are caught by
    # id-level dedupe earlier in the router, never here. DEFAULT ON: a hardening
    # phase must not silently disable an active guard; set it false to disable.
    duplicate_burst_guard_enabled: bool = True
    duplicate_burst_window_seconds: int = 5

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

    # Broker: Streaming Pro (Phase 8 / feature-streaming-pro-adapter Phase 4) — the
    # retail bridge `settrade-streaming-api` composed over plain HTTP (mirrors
    # Liberator). The engine holds ONLY the bridge's api-key + base_url: the bridge
    # owns USERNAME/PASSWORD/PIN and stamps the PIN itself, so the adapter sends NO
    # PIN. api_key is SecretStr, optional (settings must load in sim), presence-
    # checked only at runtime creation; NEVER logged. The bridge runs as a bundled
    # internal upstream (docker-compose.streaming.yml), reached on quant-network.
    streaming_pro_base_url: str = "http://streaming-pro-api:8000/api/v1"
    streaming_pro_api_key: SecretStr | None = None
    streaming_pro_heartbeat_interval_seconds: int = 30
    streaming_pro_circuit_breaker_threshold: int = 3
    streaming_pro_reconcile_interval_seconds: int = 12
    streaming_pro_post_rate_limit: int = 5

    # Venue-facing rate-limit token buckets (Phase 6 / D). These are the
    # engine-side enforcement of each venue's documented request budget, distinct
    # from the PTRM ``risk_max_orders_per_second`` gate above (that caps the
    # strategy's submit rate; these throttle outbound venue calls). On exhaustion
    # a bucket AWAITS (back-pressure) — it never drops a request nor raises to the
    # caller. ``0`` disables a bucket (unlimited). Liberator caps only the
    # placement path.
    liberator_post_rate_limit: int = 5

    # Order book service (Phase 5) — a normalized, read-only L2 cache fed by the
    # Liberator WebSocket feed (ADR D17–D24). The whole service defaults OFF
    # (D24): nothing connects, the engine's existing behavior is bit-for-bit
    # unchanged until an operator opts in. The endpoints carry no order
    # data/credential and are public-mode-readable.
    order_book_enabled: bool = False
    # LIBERATOR is the only order-book provider (operator decision 2026-06-12:
    # verified streaming live).
    order_book_primary_provider: Literal["liberator"] = "liberator"
    # JSON map symbol -> provider name, e.g. '{"AOT": "liberator"}'.
    order_book_symbol_overrides: dict[str, str] = Field(default_factory=dict)
    order_book_failover_error_threshold: int = 3
    order_book_failover_window_seconds: int = 30
    order_book_cache_max_age_seconds: int = 5
    order_book_cache_max_symbols: int = 500
    # Optional operator override: path to an extra CA PEM for the Liberator WS
    # host. The venue serves an incomplete TLS chain (leaf only) so the public
    # GlobalSign intermediate is bundled in-package; this knob covers a
    # venue-side chain rotation before a code update ships. Verification is
    # never disabled.
    order_book_liberator_extra_ca_pem: str | None = None
    # Sim fill-price fallback hop 2 (last close) + order-update stream knobs. The
    # stream_* values are consumed by the streaming sub-steps (3E–3G); they land
    # here once so settings stay a single source of truth. ``market_data_api_key``
    # is the read key for the marketdata engine (SecretStr — sent as X-API-Key,
    # NEVER logged); the base URL gates the whole fallback hop.
    market_data_base_url: str | None = None
    market_data_api_key: SecretStr | None = None
    stream_keepalive_seconds: int = 15
    stream_ring_buffer_size: int = 1024
    stream_subscriber_queue_size: int = 256

    # Informational (compose maps host port -> container :8000)
    host_port: int = 8400


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()

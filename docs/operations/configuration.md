# Operations — Configuration

Every setting is an environment variable with the prefix **`EXECUTION_ENGINE_`**, loaded via
`pydantic-settings` from the process environment and the gitignored `.env` (template: `.env.example`).
Settings are frozen at startup. **`SecretStr` values are never logged** and never appear in `/health`,
`/capabilities`, or any response — set them to `<your-value-here>` in examples, real values only in
`.env`.

> All defaults below are verified against `src/quant_execution_engine/config/settings.py`.

## Core service

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_PUBLIC_MODE` | bool | `true` | `true` disables order-submission + `/admin/*` (read-only); `false` (owner mode) opens the full surface |
| `EXECUTION_ENGINE_STAGE` | enum | `sim` | Safety ladder: `sim` \| `paper` \| `micro_live` \| `live`. `live` is gated |
| `EXECUTION_ENGINE_KILL_SWITCH_ENGAGED` | bool | `false` | Boot backstop; `true` pins the kill-switch on (a runtime disengage is refused) |
| `EXECUTION_ENGINE_HOST_PORT` | int | `8400` | Compose host-port mapping to container `:8000` (informational; uvicorn always binds `:8000`) |
| `EXECUTION_ENGINE_APP_ENV` | str | `development` | Free-form environment label |
| `EXECUTION_ENGINE_LOG_LEVEL` | str | `INFO` | Log level |

There is **no** `API_HOST` / `API_PORT` — the container always serves on `:8000`; the host mapping is
`HOST_PORT`.

## Backing stores

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_PG_DSN` | str | `postgresql://quant:quant@quant-postgres:5432/db_execution` | The durable order store DSN (least-privilege `quant` user) |
| `EXECUTION_ENGINE_PG_POOL_MIN_SIZE` | int | `1` | asyncpg pool min |
| `EXECUTION_ENGINE_PG_POOL_MAX_SIZE` | int | `10` | asyncpg pool max |
| `EXECUTION_ENGINE_REDIS_URL` | str | `redis://execution-redis:6379/0` | Own Redis sidecar (dedupe / single-flight lock / rate-limit); container `quant-execution-redis` |

## Auth

| Env var | Type | Default | SecretStr | Effect |
|---------|------|---------|:---:|--------|
| `EXECUTION_ENGINE_API_KEY` | str \| None | `None` | — | Shared `X-API-Key` (constant-time compared). When unset, a startup WARNING logs and api-key-gated reads are open |

## PTRM pre-trade risk gate

Enforced in **every** stage (including `sim`), after the capability check and before any adapter.

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_RISK_MAX_ORDER_VALUE` | Decimal | `1000000` | Per-order notional cap → `risk_rejected` 422 |
| `EXECUTION_ENGINE_RISK_MAX_ORDER_QTY` | int | `1000` | Per-order quantity cap → `risk_rejected` 422 |
| `EXECUTION_ENGINE_RISK_MAX_ORDERS_PER_SECOND` | int | `5` | Submit-rate cap → `risk_rejected` **429** (`cap=rate_limit`) |
| `EXECUTION_ENGINE_ACCOUNT_MAX_NOTIONAL` | JSON map | `{}` | Per-account notional caps, e.g. `{"ACC-1": "500000"}`; absent account ⇒ global cap |
| `EXECUTION_ENGINE_ACCOUNT_MAX_QTY` | JSON map | `{}` | Per-account quantity caps, e.g. `{"ACC-1": 500}`; absent account ⇒ global cap |
| `EXECUTION_ENGINE_RISK_DUPLICATE_BURST_WINDOW_SECONDS` | int | `2` | **Legacy** (Phase 2). Retained so old `.env` files load, **no longer read** — superseded by the unified guard below |

## Price-band advisory check

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_PRICE_BAND_ENABLED` | bool | `false` | When `true` **and** `MARKET_DATA_BASE_URL` set, a LIMIT order > `MAX_PCT`% off last close → `price_band_exceeded` 422 |
| `EXECUTION_ENGINE_PRICE_BAND_MAX_PCT` | Decimal | `10.0` | The band half-width (percent). MARKET orders bypass; a fetch failure is advisory (WARN + pass) |

## Unified duplicate-burst guard

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_DUPLICATE_BURST_GUARD_ENABLED` | bool | **`true`** | Blocks a 2nd order with the same `account\|symbol\|side\|qty\|order_type\|price` under a **different** cid in the window → `duplicate_burst_detected` 409 |
| `EXECUTION_ENGINE_DUPLICATE_BURST_WINDOW_SECONDS` | int | `5` | The burst window |

## Idempotent-submit single-flight lock

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_SUBMIT_LOCK_TTL_SECONDS` | int | `10` | Redis single-flight lock TTL on `exe:submit:{cid}` |
| `EXECUTION_ENGINE_SUBMIT_LOCK_WAIT_MS` | int | `300` | How long a concurrent identical submit waits for the in-flight row before `submit_in_flight` 409 |

## SimAdapter

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_SIM_DEFAULT_FILL_PRICE` | Decimal | `100` | Reference fill price for MARKET/MTL/ATO/ATC sim orders with no price (last hop of the pricing chain) |

## Order-update stream

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_STREAM_KEEPALIVE_SECONDS` | int | `15` | SSE keep-alive comment interval |
| `EXECUTION_ENGINE_STREAM_RING_BUFFER_SIZE` | int | `1024` | `Last-Event-ID` replay window |
| `EXECUTION_ENGINE_STREAM_SUBSCRIBER_QUEUE_SIZE` | int | `256` | Per-subscriber back-pressure bound (drop-oldest → `gap` marker) |

## Order book service (default-off)

| Env var | Type | Default | Effect |
|---------|------|---------|--------|
| `EXECUTION_ENGINE_ORDER_BOOK_ENABLED` | bool | `false` | Master switch; off ⇒ no provider connects, endpoints 404 |
| `EXECUTION_ENGINE_ORDER_BOOK_PRIMARY_PROVIDER` | enum | `liberator` | `liberator` — the sole provider since the 2026-07-18 Settrade removal |
| `EXECUTION_ENGINE_ORDER_BOOK_SYMBOL_OVERRIDES` | JSON map | `{}` | Pin symbols to a provider, e.g. `{"AOT": "liberator"}` (an override never fails over) |
| `EXECUTION_ENGINE_ORDER_BOOK_FAILOVER_ERROR_THRESHOLD` | int | `3` | Consecutive active-provider errors before failover |
| `EXECUTION_ENGINE_ORDER_BOOK_FAILOVER_WINDOW_SECONDS` | int | `30` | Sliding window the error count is measured over |
| `EXECUTION_ENGINE_ORDER_BOOK_CACHE_MAX_AGE_SECONDS` | int | `5` | A snapshot older than this reads as absent (stale) |
| `EXECUTION_ENGINE_ORDER_BOOK_CACHE_MAX_SYMBOLS` | int | `500` | LRU capacity |
| `EXECUTION_ENGINE_ORDER_BOOK_LIBERATOR_EXTRA_CA_PEM` | str \| None | `None` | Optional extra CA PEM path for the Liberator WS host (TLS verification never disabled) |

## Market data (price band + sim fill pricing)

| Env var | Type | Default | SecretStr | Effect |
|---------|------|---------|:---:|--------|
| `EXECUTION_ENGINE_MARKET_DATA_BASE_URL` | str \| None | `None` | — | The market-data engine URL; gates the price-band check + the sim last-close fallback hop |
| `EXECUTION_ENGINE_MARKET_DATA_API_KEY` | SecretStr \| None | `None` | ✅ | `X-API-Key` for the market-data engine (never logged) |

## Liberator adapter

| Env var | Type | Default | SecretStr | Effect |
|---------|------|---------|:---:|--------|
| `EXECUTION_ENGINE_LIBERATOR_BASE_URL` | str | `http://liberator-trading-api:8200/api/v1` | — | Internal upstream URL (never `localhost`) |
| `EXECUTION_ENGINE_LIBERATOR_API_KEY` | SecretStr \| None | `None` | ✅ | api-key header to the upstream (must equal its `API_KEY`) |
| `EXECUTION_ENGINE_LIBERATOR_HEARTBEAT_INTERVAL_SECONDS` | int | `30` | — | Session heartbeat cadence |
| `EXECUTION_ENGINE_LIBERATOR_CIRCUIT_BREAKER_THRESHOLD` | int | `3` | — | Consecutive heartbeat failures before the breaker trips |
| `EXECUTION_ENGINE_LIBERATOR_RECONCILE_INTERVAL_SECONDS` | int | `12` | — | Reconciliation loop cadence |
| `EXECUTION_ENGINE_HANDLE_RECOVERY_CADENCE_MS` | int | `250` | — | Post-placement burst cadence (TK-0423). ≈ the bridge read latency (200–244 ms) — polling faster cannot produce a fresher answer, it only loads a venue session shared with the capture plane |
| `EXECUTION_ENGINE_HANDLE_RECOVERY_MIN_POLLS` | int | `3` | — | **Retry floor** (TK-0426). At least this many venue reads run, counted from whenever recovery starts. It exists because the deadline is submit-anchored, so a slow placement used to leave ~1 retry — making `unknown` ("the venue could not be read") reachable because **our own call** was slow. 🔴 Checked BEFORE the deadline and ANDed with it: the ceiling can never cut below the floor |
| `EXECUTION_ENGINE_HANDLE_RECOVERY_DEADLINE_MS` | int | `1500` | — | **Submit-anchored ceiling**; may only end the burst *after* `MIN_POLLS` is satisfied. Bounds total submit-to-known inside the operator's 2 s bar: 3 × 250 ms on top of the worst observed placement (1175 ms) = 1925 ms. On expiry the submit reports `resolution: pending`/`unknown` instead of blocking |
| `EXECUTION_ENGINE_LIBERATOR_POST_RATE_LIMIT` | int | `5` | — | Token bucket on `place()` only (req/s); `0` = unlimited |

## Streaming Pro adapter

The `StreamingProAdapter` composes the bundled `settrade-streaming-api` retail bridge over plain
HTTP; the engine holds **only** the bridge api-key — the bridge owns USERNAME/PASSWORD/PIN and
stamps the PIN itself, so **no engine-side PIN**. (The Settrade Open-API `EXECUTION_ENGINE_SETTRADE_*`
settings — broker-023 / `settrade_v2` — were **removed on 2026-07-18**.)

| Env var | Type | Default | SecretStr | Effect |
|---------|------|---------|:---:|--------|
| `EXECUTION_ENGINE_STREAMING_PRO_BASE_URL` | str | `http://streaming-pro-api:8000/api/v1` | — | Internal bridge URL (never `localhost`) |
| `EXECUTION_ENGINE_STREAMING_PRO_API_KEY` | SecretStr \| None | `None` | ✅ | api-key to the bridge (must equal the bridge's `STREAMING_PRO_API_KEY`); required for the runtime to start |
| `EXECUTION_ENGINE_STREAMING_PRO_HEARTBEAT_INTERVAL_SECONDS` | int | `30` | — | `/session/status` heartbeat cadence |
| `EXECUTION_ENGINE_STREAMING_PRO_CIRCUIT_BREAKER_THRESHOLD` | int | `3` | — | Consecutive failures before the breaker trips |
| `EXECUTION_ENGINE_STREAMING_PRO_RECONCILE_INTERVAL_SECONDS` | int | `12` | — | Reconciliation loop cadence |
| `EXECUTION_ENGINE_STREAMING_PRO_POST_RATE_LIMIT` | int | `5` | — | Token bucket on placement (req/s); `0` = unlimited |

## Bundled Liberator upstream (overlay only)

When the `docker-compose.liberator.yml` overlay runs, the `liberator-trading-api` **container** reads
its own (un-prefixed) credentials from the same `.env`: `LIBERATOR_USERNAME`, `LIBERATOR_PASSWORD`,
`LIBERATOR_PIN`, `TEL_NO` (OTP phone), `API_KEY`, optional `REDIS_PASSWORD`. These are the upstream's
secrets — distinct from the engine's `EXECUTION_ENGINE_LIBERATOR_*`. Never commit `.env`.

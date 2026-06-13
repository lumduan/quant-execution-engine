# Architecture — Security Boundary

This service is the platform's only holder of broker order-routing credentials and the only place a
real order can originate. The boundary has four parts: **credential ownership**, **public vs owner
mode**, the **pre-trade risk gate + kill-switch**, and **logging redaction**. For the stage ladder
see [`overview.md`](overview.md); for the operator procedures see
[`../operations/kill-switch.md`](../operations/kill-switch.md) and
[`../operations/configuration.md`](../operations/configuration.md).

## Credential ownership

Broker credentials live **only** in this service's gitignored `.env` — never committed, never logged,
never held by a strategy, the gateway, or a host. All are `SecretStr`.

| Broker | Secrets (`EXECUTION_ENGINE_*`) |
|--------|--------------------------------|
| Liberator | `LIBERATOR_API_KEY`, `LIBERATOR_PIN` |
| Settrade (shared) | `SETTRADE_APP_ID`, `SETTRADE_APP_SECRET`, `SETTRADE_PIN` |
| Settrade (per-market, Phase 4.1) | `SETTRADE_EQUITY_APP_{ID,SECRET}`, `SETTRADE_DERIVATIVES_APP_{ID,SECRET}` |
| Market data (price band / sim) | `MARKET_DATA_API_KEY` |

Presence is checked only at runtime adapter creation — settings still load in `sim` (no broker
configured). Strategies submit a `NormalizedOrder` through the gateway and never hold a credential;
the gateway proxies and holds none.

## Public vs owner mode

`EXECUTION_ENGINE_PUBLIC_MODE` (default `true`, the Docker default) is the coarse surface gate,
enforced by the `require_owner_mode` dependency (typed `public_mode`, 403):

| Endpoint | Public mode | Owner mode |
|----------|:---:|:---:|
| `GET /health`, `GET /capabilities` | ✅ | ✅ |
| `GET /orders/{cid}`, `GET /orders/stream` | ✅ | ✅ |
| `GET /order-book/{symbol}[/stream]` | ✅ | ✅ |
| `POST` / `DELETE` / `PATCH /orders` | ✗ 403 | ✅ |
| `POST /admin/kill-switch/{engage,disengage}`, `GET /admin/kill-switch` | ✗ 403 | ✅ |
| `GET /admin/orders/{cid}/audit`, `GET /admin/audit/export` | ✗ 403 | ✅ |

Owner mode is the only mode that holds broker credentials. The read/stream endpoints are additionally
**api-key-gated** (`X-API-Key`, constant-time compared when `EXECUTION_ENGINE_API_KEY` is set).

## The PTRM pre-trade risk gate

Every submit — in **every** stage, including `sim` (the risk gate is not mode-dependent) — passes the
`RiskGate` after the capability check and before any adapter routing:

| Control | Env (`EXECUTION_ENGINE_*`) | Reject |
|---------|----------------------------|--------|
| Per-order value cap | `RISK_MAX_ORDER_VALUE` (default `1000000`) | `risk_rejected` 422 (`cap=value`) |
| Per-order qty cap | `RISK_MAX_ORDER_QTY` (default `1000`) | `risk_rejected` 422 (`cap=qty`) |
| Submit rate cap | `RISK_MAX_ORDERS_PER_SECOND` (default `5`) | `risk_rejected` **429** (`cap=rate_limit`) |
| Per-account notional cap | `ACCOUNT_MAX_NOTIONAL` (JSON map, default `{}`) | `risk_rejected` 422 (`cap=notional`) |
| Per-account qty cap | `ACCOUNT_MAX_QTY` (JSON map, default `{}`) | `risk_rejected` 422 (`cap=qty`) |
| Duplicate-burst guard | `DUPLICATE_BURST_GUARD_ENABLED` (default **`true`**) + `DUPLICATE_BURST_WINDOW_SECONDS` (`5`) | `duplicate_burst_detected` 409 |

Per-account caps **bind when the account is present** in the map and **fall back to the global cap**
otherwise — never a silent skip. The **duplicate-burst guard** blocks a second order carrying the
same economic fingerprint `account|symbol|side|quantity|order_type|price` under a **different**
`client_order_id` within the window (a same-`cid` resend is caught earlier by idempotency dedupe, not
here). It is **default-ON** — a hardening phase must not silently disable an active guard.

## The advisory price-band check

`PRICE_BAND_ENABLED` (default **`false`**) + `PRICE_BAND_MAX_PCT` (default `10.0`): when enabled **and**
`MARKET_DATA_BASE_URL` is configured, a `LIMIT` order whose price deviates from the symbol's last
close by more than `MAX_PCT` percent is rejected (`price_band_exceeded`, 422). `MARKET` orders bypass;
a market-data fetch failure is **advisory** (WARN + pass). It runs after the PTRM gate and before any
adapter routing — the kill-switch-first invariant is preserved.

## The kill-switch

The hardest stop. `EXECUTION_ENGINE_KILL_SWITCH_ENGAGED` is the boot backstop; the runtime trip is
`POST /admin/kill-switch/engage`. Engaging:

1. **Rejects all new submits** (`kill_switch_engaged`, 503) — checked **first** in the submit path,
   and in the **amend** path (an amend can increase exposure). The **cancel** path is deliberately
   **not** gated — a cancel reduces risk.
2. **Mass-cancels every open order** (flatten-and-halt), reporting `cancelled_count`.
3. Writes a **structured JSON audit log** (`kill_switch.engaged`) carrying the optional `X-Operator-Id`.

Engage is **idempotent** (a second engage returns `already_engaged=true`, no second sweep). Disengage
is a deliberate operator action (`POST /admin/kill-switch/disengage`; `409 kill_switch_not_engaged`
if already clear). When the env flag pins the switch on at boot, a runtime disengage is refused
(`409 kill_switch_env_pinned`). See [`../operations/kill-switch.md`](../operations/kill-switch.md).

The per-adapter **circuit breaker** is independent of the kill-switch: it trips **automatically** on
consecutive heartbeat failures (`broker_circuit_open`, 503), whereas the kill-switch is
**operator-controlled**. Both end the same way for open orders — a mass-cancel.

## Logging redaction

Account numbers, OAuth tokens, PINs, API keys, and ECDSA signatures are **never logged**. Secrets are
`SecretStr` (their `repr` is masked); the broker transports redact account/PIN before emitting; the
Settrade httpx logger is demoted to `WARNING` because the account rides the request URL path. Raw
broker payloads never cross the public boundary. Never add a log line that interpolates a credential,
an account number, or a full order payload.

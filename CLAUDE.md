# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this
repository.

## Project

`quant-execution-engine` is the platform's **Execution engine** — a standalone `EXTERNAL`
engine that is the **single canonical order router** and the **sole owner of broker
order-routing credentials**. It is a FastAPI service on container port `:8000` (host port
`:8400`) that joins the external **`quant-network`** and is **proxied by `quant-api-gateway`**
under `/api/v2/engines/execution/*`. It writes a durable order store (`execution.*` in
`quant-infra-db`/TimescaleDB) and ships its **own Redis sidecar** (dedupe / single-flight
submit lock / rate-limit).

> **Current state: Phases 0–7 complete (Phase 4 + 4.1: 2026-06-11; Phase 5 engine side +
> Phase 5.1 strategy side: 2026-06-12; Phase 6 safety/ops/reconciliation hardening: 2026-06-13;
> Phase 7 documentation hub: 2026-06-13).** Phase 0:
> ADR ACCEPTED — the contracts (D1–D13, `NormalizedOrder`, `BrokerAdapter`, state machine,
> capability-matrix shape) are **frozen** in the umbrella ADR
> [`.claude/knowledge/feature-execution-engine.md`](../.claude/knowledge/feature-execution-engine.md).
> Phase 1: the durable order store is **live** (`db_execution`/`execution.*` in
> `quant-infra-db`, trigger-enforced frozen state machine + append-only audit). Phase 2:
> the **engine core + deterministic `SimAdapter` + gateway proxy are live** — the full sim
> order path (`POST/GET/DELETE /orders`, `/capabilities`, owner-mode `/admin/kill-switch*`)
> runs end-to-end through `/api/v2/engines/execution/*` with idempotent dedupe, PTRM caps,
> and the kill-switch wired first in the submit path. Phase 3: **`LiberatorAdapter` — the
> first real venue** — composes the bundled `liberator-trading-api` over HTTP (D9):
> SET/TFEX payload mapping, redacting transport (PIN/account never logged), reconciliation
> loop v1 (§B lost-ack fuzzy match, bounded resolution), ~30 s session heartbeat + circuit
> breaker (trip ⇒ `broker_circuit_open` + mass-cancel; state on `/health`/`/capabilities`),
> stage matrix (`paper` intercepts placement to sim with the session live; `micro_live`
> routes `broker=liberator` real at PTRM-capped size), router-level cancel+replace amend,
> `EXECUTION_ENGINE_LIBERATOR_*` settings (`SecretStr`). Phase 4: **`SettradeAdapter` — the
> second real venue, proving the abstraction** — full **SET equity + TFEX derivatives** over a
> raw `httpx.AsyncClient` OAuth client (NOT the sync `settrade-v2` SDK): ECDSA P-256 login
> signing, single-flight `ensure_token()` with proactive refresh + refresh-fail→fresh-login +
> one reactive-401 retry; **native amend** over the frozen `PENDING_REPLACE → NEW` edge (one
> atomic `replace_order`; venue reject = non-terminal restore + typed `AmendRejected` 409) via
> a new **`PATCH /orders/{client_order_id}`** route (native amends in place / cancel_replace
> returns the replacement cid); OAuth token-liveness heartbeat + breaker (no venue health
> endpoint), reconciler v1 (mirrors Liberator; watermark fills + a new `replace_resolve` action
> for stranded `PENDING_REPLACE`; observe-don't-throttle rate budget); stage matrix
> (`paper` intercepts placement to sim / `micro_live` routes `broker=settrade` real); capability
> cells pinned from `developer.settrade.com`; `EXECUTION_ENGINE_SETTRADE_*` settings
> (`SecretStr` creds/PIN); `cryptography>=42` dep; **no compose overlay** (cloud API — creds ride
> `docker-compose.private.yml`'s `env_file`). **Phase 4.1 (2026-06-11): per-market broker apps** —
> `SettradeAdapter` holds one `SettradeClient` per market (ctor `clients: Mapping[Market,
> SettradeClient]`) behind the unchanged `NormalizedOrder`, so a broker that splits its books across
> two OAuth apps (the real broker **InnovestX `023`**: `ALGO_EQ` = SET equity, `ALGO` = TFEX
> derivatives) routes both legs of a stock-vs-futures spread concurrently; six per-market settings
> (`EXECUTION_ENGINE_SETTRADE_{EQUITY,DERIVATIVES}_APP_{ID,SECRET,CODE}`), per-market
> credentials-resolution with partial-trio-fails-loud (no silent shared fallback) and a shared-trio
> single-app fallback (the UAT sandbox), all-sessions heartbeat on the single frozen breaker (one
> dead app trips + mass-cancels both books), per-market reconciler budget skip, additive `/health`
> `brokers.settrade.sessions`; **real-venue validated read-only** against prod broker 023 (the
> InnovestX trading PIN is still absent from `.env` — the explicit `micro_live`-flip prerequisite).
> A spread is two independent `POST /orders` (no batch endpoint); an in-engine refactor (no new
> `third_party` service). (Plans:
> [`docs/plans/phase1-execution-order-store.md`](docs/plans/phase1-execution-order-store.md),
> [`docs/plans/phase2-engine-core-simadapter.md`](docs/plans/phase2-engine-core-simadapter.md),
> [`docs/plans/phase3-liberator-adapter.md`](docs/plans/phase3-liberator-adapter.md),
> [`docs/plans/phase4-settrade-adapter.md`](docs/plans/phase4-settrade-adapter.md),
> [`docs/plans/phase4.1-settrade-per-market-apps.md`](docs/plans/phase4.1-settrade-per-market-apps.md).)
> **Phase 5 (2026-06-12): engine side complete — the normalized order-update stream out + a
> dual-provider order book service.** The engine now pushes the **normalized order-update stream
> out** (umbrella **D12** realised): `GET /orders/stream` (SSE; `id:`=seq / `event:`=engine-state
> frames — a strict 9-state subset + `gap`/`resync_required` advisories; `strategy_id`/`client_order_id`
> filters; `Last-Event-ID` ring-buffer replay) fed by an in-process `EventHub` whose `publish`
> hooks sit in the **five** repository writers (`insert_order`/`ack_order`/`replace_order`/`update_status`/`apply_fill`)
> every one of the 13 frozen edges funnels through — post-success, non-blocking, exception-proof
> (**the stream is advisory, the durable store is truth**). `X-Strategy-Id` (D16) stamps a new
> nullable `execution.orders.strategy_id` (`quant-infra-db` PR #15) so stream filtering survives
> restarts (DB-seeded). A new **in-engine, read-only order book service** (D17) normalizes a
> **dual-provider** L2 feed — **Settrade** realtime (`settrade-v2` SDK contained behind a lazy
> import + `asyncio.to_thread`, **E21 order-routing SDK ban unchanged**) + **Liberator** ws-ticket
> + raw `websockets` Engine.IO v4 client (**no `curl_cffi`**) — with consecutive-error failover
> (D20), an LRU cache, `GET /order-book/{symbol}[/stream]`, and an additive `/health` `order_book`
> block; it feeds `SimAdapter` **live fill prices** (D21: book best bid/offer → market-data last
> close → reference, all limit-bounded; **with no source injected the sim is bit-for-bit Phase-2**).
> All **additive and default-off** (`ORDER_BOOK_ENABLED=false`): the broker-free `docker compose up`
> default is bit-for-bit unchanged. Decisions **D14–D24** (the cross-cutting D-series continuation,
> not the E-series — streaming is umbrella D12); open questions **§H–§K**. **`live`/`micro_live`
> gating, the kill-switch, PTRM, and the frozen `NormalizedOrder` / state machine / capability
> cells are unchanged.** 853 tests, 95.72% cov. The strategy-side scope (the `*_EXECUTION_MODE`
> flags + the end-to-end sim trade loop) split to **Phase 5.1** by operator decision. (Plan:
> [`docs/plans/phase5-strategy-execution-path-order-streaming.md`](docs/plans/phase5-strategy-execution-path-order-streaming.md).)
> **Phase 5.1 (2026-06-12): strategy side complete — both strategies are first-class callers**
> (no engine code change; this repo's PR was docs-only): `csm-set` PR #16 (`CSM_EXECUTION_MODE`,
> 72 new tests, new modules 96–100% cov) and `tfex-s50-multi-tf-swing` PR #18
> (`TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE`, 81 new tests, 97.83% cov) each gained local wire
> mirrors + an `ExecutionEngineAdapter` (gateway-only, `X-API-Key` + `X-Strategy-Id`, same-cid
> transport retry, SSE with `Last-Event-ID` reconnect + seq watermark) + a `run_sim_loop`
> (subscribe-before-submit, single-source fill accounting, GET-residual reconcile; tfex infers
> `position_effect` OPEN/CLOSE against the evolving position) — **library + verify-script only**,
> default `off`, `live` rejected under strategy public mode; gateway PR #24 now forwards
> `X-Strategy-Id` (it was being stripped — D16 attribution proven live in `execution.orders`).
> Live-verified end-to-end in sim 2026-06-12 (csm PTT 100 @ 35.50 FILLED via SSE; tfex S50Z2026
> OPEN→CLOSE→flat). (Plan:
> [`docs/plans/phase5.1-strategy-execution-flags-sim-trade-loop.md`](docs/plans/phase5.1-strategy-execution-flags-sim-trade-loop.md);
> consumer contract: [`.claude/knowledge/order-update-stream.md`](.claude/knowledge/order-update-stream.md).)
> **Phase 6 (2026-06-13): safety, ops & reconciliation hardening — failure paths made provably
> safe under fault injection** (no new feature, no new broker; everything additive behind the
> unchanged frozen contracts). Five workstreams: **(A) risk-gate hardening** — per-account
> notional/qty caps (`EXECUTION_ENGINE_ACCOUNT_MAX_{NOTIONAL,QTY}` JSON maps; an absent account
> falls back to the global cap, never a silent skip; enforced in EVERY stage incl. `sim`); an
> **advisory price-band check** (`core/price_band.py`, reuses a factored-out shared
> `adapters/market_data.py` `MarketDataClient`, wired AFTER the PTRM gate, MARKET bypass, WARN+pass
> on fetch failure, typed `PriceBandExceeded` 422, default-off); and the **unified
> duplicate-burst guard** (one guard, richer fingerprint `account|symbol|side|qty|order_type|price`,
> `exe:burst:` Redis key, typed `DuplicateBurstDetected` 409, **default-ON** — a hardening phase
> must not silently disable an active guard, so the prior always-on coarse 2 s/429 guard is
> superseded). **(B) kill-switch admin-trip hardening** — idempotent engage/disengage (engage
> twice → `already_engaged=true`, no second sweep; disengage when clear → 409
> `kill_switch_not_engaged`), structured JSON `kill_switch.engaged|disengaged` audit logs, optional
> `X-Operator-Id` (`anonymous` default), `cancelled_count`; a 5-order (NEW + PARTIALLY_FILLED)
> fault-injection test asserting genuine CANCELLED-transition audit rows + the structured log.
> **(C) idempotency soak + reconciliation drift** — PENDING_NEW-stuck / ack-lost / fill-before-ack /
> same-cid-retry scenarios (no double-send under a mid-submit kill); DB-behind repairs to FILLED,
> DB-ahead never regresses a terminal, stranded `PENDING_REPLACE` `replace_resolve`. **(D)
> per-adapter rate limits** — a pure-asyncio `adapters/rate_limit.py` `TokenBucket` (monotonic
> lazy-refill, await-on-deficit, one WARN/wait, never drop/raise, `rate<=0` ⇒ no-op): Settrade
> GET + WRITE buckets per `SettradeClient` (per OAuth app/market, not per-adapter) + a Liberator
> POST bucket on `place()` only; plus an `EventHub` §H slow-subscriber stress test (1000 ev/s ×
> 10 slow subscribers — **single-process confirmed, no fan-out code change**). **(E) structured
> audit** — owner-mode `GET /admin/orders/{cid}/audit` + streaming NDJSON
> `GET /admin/audit/export` (`from_ts`/`to_ts`/`strategy_id` filters, server-side cursor), the
> response **synthesized** from the existing `order_events` columns — **NO infra-db schema
> change** (`seq`/`event_type`/`broker_order_id`/`metadata`/`occurred_at` derived at read time).
> **`live` stays gated — no real-money default; the frozen `NormalizedOrder` / 13-edge state
> machine / capability cells, kill-switch-first ordering, and PTRM semantics are all unchanged;
> no infra-db schema change.** 952 tests, 96.01% cov, mypy strict, ruff clean. (Plan:
> [`docs/plans/phase6-safety-ops-reconciliation-hardening.md`](docs/plans/phase6-safety-ops-reconciliation-hardening.md).)
> **`live` stays gated — no real-money default**; real micro_live venue validation is
> operator-driven (Liberator OTP login / Settrade OAuth app creds; see the safety playbook's
> Liberator + Settrade runbooks). Build sequence:
> [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md) (8 phases, 0–7). **Phase 7 ✓ (2026-06-13)** —
> the documentation hub (`docs/` + `.claude/` refresh, tvkit-ref style, AI-agent-first); start at
> [`docs/README.md`](docs/README.md).

### Ownership boundaries (the whole point of this service)

1. **Sole broker-credential owner.** Only this service holds broker order-routing sessions
   (Liberator OTP/PIN, Settrade OAuth `app_id`/`app_secret`/`app_code`). No strategy, no
   gateway, and no host holds them. Secrets live **only** in this service's gitignored `.env`
   — never committed, never logged.
2. **Canonical order router.** Strategies submit one `NormalizedOrder`; the engine routes it
   to a `BrokerAdapter` (Liberator, Settrade) or `SimAdapter`. Add a broker = write one
   adapter, not touch every strategy.
3. **Gateway-proxied.** Consumers (strategies, OpenBB) call the gateway's
   `/api/v2/engines/execution/*`; the gateway proxies to `:8400` and holds **no** credential.
4. **Strategies never speak a broker API.** They POST normalized orders behind a flag and
   react to the normalized order-update stream; they never hold a credential.

## The two planes (do not merge them)

This is the **execution / order-command plane only** (low-volume, real-money, idempotent,
durable). The **market-data / streaming plane** (ticks, order book, OBI replay) is a separate
concern that stays in `order-book-infrastructure` + `quant-marketdata-engine`. We may *read*
those feeds for price-band pre-trade checks; we never own them. Rationale: a dropped tick is a
resubscribe, a duplicated order is a real loss.

## `NormalizedOrder` contract + status enum (frozen in Phase 0)

`NormalizedOrder(client_order_id, broker, account, market=SET|TFEX, symbol, side=BUY|SELL,
order_type=MARKET|LIMIT|STOP|STOP_LIMIT|ICEBERG|MTL|ATO|ATC, price?, stop_price?, quantity,
display_qty?, tif=DAY|IOC|FOK|GTC, position_effect?=OPEN|CLOSE)`. Status enum:
`NEW | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | EXPIRED`. `Decimal`-as-string on the
wire; UTC timestamps. Full sketch + state machine: [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md)
and [`.claude/knowledge/normalized-order-contract.md`](.claude/knowledge/normalized-order-contract.md).

## Safety ladder (`EXECUTION_ENGINE_STAGE`) — the most important rule

`sim` (default) → `paper` → `micro_live` → `live`. **No order reaches a real broker below
`micro_live`, and never without owner mode on, the kill-switch disengaged, and the broker
runtime configured.** Since Phases 3–4 (Liberator + Settrade): `paper` keeps each configured
broker session live for reads but intercepts every placement to sim; `micro_live` routes
`broker=liberator`/`broker=settrade` to the real venue at PTRM-capped size; **`live` stays
gated** (typed reject). The kill-switch (`EXECUTION_ENGINE_KILL_SWITCH_ENGAGED`) overrides
every stage and is checked first in the submit path — including the **native-amend path**,
which (unlike the un-gated cancel path) is kill-switch-gated up front because an amend can
*increase* exposure. Public mode (`EXECUTION_ENGINE_PUBLIC_MODE=true`, Docker default)
disables all order-submission endpoints. See the ROADMAP's "Safety ladder" section and the
safety playbook's Liberator + Settrade runbooks (stage-flip rule, breaker trips, native
amend).

## Network & ports (`quant-network`)

| Item | Value |
|---|---|
| Service hostname (in-container) | `quant-execution-engine` |
| Container port | `:8000` (always — like every other service) |
| Host port | `:8400` |
| Health check | `curl http://localhost:8400/health` |
| Durable order store | `quant-postgres:5432` (`execution.*`, `db_execution`) |
| Own Redis sidecar | in this repo's compose (`quant-execution-redis`, distinct from the gateway's Redis) |
| Gateway proxy surface | `POST\|GET\|DELETE\|PATCH /api/v2/engines/execution/*` (orders, status, capabilities, native amend) + **SSE streams** `GET /orders/stream` and `GET /order-book/{symbol}[/stream]` (non-buffering pass-through) |

Use **service hostnames inside containers**, not `localhost`. Host ports exist only for
developer access.

## Commands

Everything runs through `uv`. Never call `python` / `pip` / `poetry` / `conda` directly.

```bash
uv sync --all-groups                                  # install deps (incl. dev)
uv run pytest                                         # full test suite + coverage gate
uv run ruff check .                                   # lint
uv run ruff format --check .                          # format check (passive)
uv run mypy src tests                                 # strict type check
uv run uvicorn src.quant_execution_engine.api.main:app --port 8000   # run the API
```

Combined quality gate (must pass before every push, matching CI):

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
```

Docker:

```bash
docker compose up                                                     # public mode, host :8400
docker compose -f docker-compose.yml -f docker-compose.private.yml up # owner mode (broker creds via .env)
# Owner mode + bundled Liberator upstream (internal-only; broker-free without it):
docker compose -f docker-compose.yml -f docker-compose.private.yml -f docker-compose.liberator.yml up -d
```

Liberator engine-side env (`EXECUTION_ENGINE_` prefix, see `.env.example`):
`LIBERATOR_BASE_URL` (default `http://liberator-trading-api:8200/api/v1`),
`LIBERATOR_API_KEY` + `LIBERATOR_PIN` (SecretStr — required for the runtime to start),
`LIBERATOR_HEARTBEAT_INTERVAL_SECONDS=30`, `LIBERATOR_CIRCUIT_BREAKER_THRESHOLD=3`,
`LIBERATOR_RECONCILE_INTERVAL_SECONDS=12`.

Settrade engine-side env (`EXECUTION_ENGINE_` prefix, see `.env.example`): Settrade is a
**cloud API — no compose overlay**; creds ride `docker-compose.private.yml`'s `env_file`.
`SETTRADE_BASE_URL` (prod `https://open-api.settrade.com`; UAT `https://open-api-test.settrade.com`,
sandbox `BROKER_ID=098`), `SETTRADE_APP_ID` + `SETTRADE_APP_SECRET` + `SETTRADE_PIN` (SecretStr),
`SETTRADE_APP_CODE` + `SETTRADE_BROKER_ID` — `broker_id` + `pin` + at least one market's app trio
required for the runtime to start (`SETTRADE_ACCOUNT_NO` is an integration-test convenience only;
the per-order account comes from `NormalizedOrder.account`). **Per-market broker apps (Phase 4.1)** —
optional overrides so a broker can split its books across two OAuth apps (InnovestX `023`:
`ALGO_EQ` = SET, `ALGO` = TFEX): `SETTRADE_EQUITY_APP_ID` + `SETTRADE_EQUITY_APP_SECRET` +
`SETTRADE_EQUITY_APP_CODE` (SET), `SETTRADE_DERIVATIVES_APP_ID` + `SETTRADE_DERIVATIVES_APP_SECRET` +
`SETTRADE_DERIVATIVES_APP_CODE` (TFEX); a market with no override falls back to the shared
`SETTRADE_APP_*` trio, a PARTIAL per-market trio fails loud (market unconfigured + WARNING, no silent
fallback). `SETTRADE_HEARTBEAT_INTERVAL_SECONDS=30`,
`SETTRADE_CIRCUIT_BREAKER_THRESHOLD=3`, `SETTRADE_RECONCILE_INTERVAL_SECONDS=12`,
`SETTRADE_TOKEN_REFRESH_MARGIN_SECONDS=100`.

Order book + streaming env (Phase 5; `EXECUTION_ENGINE_` prefix, see `.env.example`): the order
book service is **additive and default-off** — `ORDER_BOOK_ENABLED=false` (master switch, D24)
keeps the engine bit-for-bit unchanged; enabling it also needs at least one configured provider
(reusing the Liberator api-key / per-market Settrade trios). `ORDER_BOOK_PRIMARY_PROVIDER=liberator` (default; Settrade realtime is venue-gated until enabled at the InnovestX portal)
(`settrade|liberator`), `ORDER_BOOK_SYMBOL_OVERRIDES={}` (JSON symbol→provider),
`ORDER_BOOK_FAILOVER_ERROR_THRESHOLD=3` + `ORDER_BOOK_FAILOVER_WINDOW_SECONDS=30` (consecutive-error
failover, D20), `ORDER_BOOK_CACHE_MAX_AGE_SECONDS=5` + `ORDER_BOOK_CACHE_MAX_SYMBOLS=500` (LRU).
`MARKET_DATA_BASE_URL` (optional — the `SimAdapter` last-close fallback hop, D21) +
`MARKET_DATA_API_KEY` (SecretStr). The order-update stream knobs: `STREAM_KEEPALIVE_SECONDS=15`
(SSE comment interval), `STREAM_RING_BUFFER_SIZE=1024` (`Last-Event-ID` replay window),
`STREAM_SUBSCRIBER_QUEUE_SIZE=256` (per-subscriber back-pressure bound). New deps: `websockets`,
`settrade-v2` (lazy, market-data-only). **`live`/`micro_live` gating is unchanged** — these feeds
are read-only market data.

Safety / ops env (Phase 6; `EXECUTION_ENGINE_` prefix, see `.env.example`) — all additive,
default-safe; the frozen contracts and gating are unchanged. **Risk-gate hardening:**
`ACCOUNT_MAX_NOTIONAL={}` (JSON map account→Decimal-as-string) + `ACCOUNT_MAX_QTY={}` (JSON map
account→int) — per-account PTRM caps; an account present binds to its own cap, an absent account
falls back to the global `RISK_MAX_*` cap (never a silent skip), enforced in EVERY stage incl.
`sim`. `PRICE_BAND_ENABLED=false` + `PRICE_BAND_MAX_PCT=10.0` — the advisory price-band check (when
on AND `MARKET_DATA_BASE_URL` is set, a LIMIT order more than MAX_PCT off the symbol's last close
is rejected 422; MARKET bypasses; a market-data fetch failure is advisory WARN+pass).
`DUPLICATE_BURST_GUARD_ENABLED=true` + `DUPLICATE_BURST_WINDOW_SECONDS=5` — the unified
duplicate-burst guard (a second order with the same `account|symbol|side|qty|order_type|price`
fingerprint under a DIFFERENT cid inside the window → 409; same-cid resends stay idempotency
dedupe). **Default ON** (a hardening phase must not silently disable an active guard); the legacy
`RISK_DUPLICATE_BURST_WINDOW_SECONDS` is retained so old `.env` files load but is **no longer read**
by the guard. **Venue rate limits** (`adapters/rate_limit.py` `TokenBucket`; on exhaustion the
bucket awaits — never drops/raises; `0` = unlimited): `SETTRADE_POST_RATE_LIMIT=10` +
`SETTRADE_GET_RATE_LIMIT=10` — Settrade WRITE (POST/PATCH) vs GET buckets **per `SettradeClient`**
(per OAuth app/market, not per-adapter); `LIBERATOR_POST_RATE_LIMIT=5` — a POST bucket on
`place()` only (cancel/heartbeat/reconciler fetches stay unthrottled). The audit reads
(`GET /admin/orders/{cid}/audit`, `GET /admin/audit/export`) add **no** env var — owner-mode,
synthesized from the existing `order_events` store (no schema change).

## Quality gates

`ruff` (E, F, I, UP, B, SIM) · `mypy --strict` · `pytest` with **≥90% coverage on core
modules** (`--cov-fail-under=90`), enforced in CI and `pyproject.toml`. As the order path
lands, ≥90% applies specifically to `adapters/` + the order state machine.

## Bring-up order (relative to infra-db & gateway)

```
quant-infra-db          # creates quant-network + Postgres/TimescaleDB (must be first)
quant-execution-engine  # this service + its own Redis sidecar (host :8400)
quant-api-gateway       # proxies /api/v2/engines/execution/* → :8400
strategies (csm-set, tfex-s50-multi-tf-swing)   # submit normalized orders behind a flag
```

Tear down in reverse; only `quant-infra-db` down removes `quant-network`.

## Hard rules — service-specific

1. **Broker credentials live only here**, in a gitignored `.env`. **Never commit; never log.**
   The gateway and strategies hold none.
2. **Idempotency is mandatory.** Every order carries a client-generated `client_order_id`; the
   engine dedupes **before** routing. Re-submitting the same id returns the prior ack.
3. **Kill-switch overrides everything** and is checked first in the submit path. Sim is the
   default stage; `live` is gated and off by default.
4. **Adapters declare capabilities; the router enforces them.** Reject unsupported
   `(broker, market, order_type, tif)` up front with a typed error — never fail silently at a
   venue. Liberator has **no amend route** → `LiberatorAdapter.amend` is cancel-then-replace
   (declared, non-atomic); Settrade amends **natively** (Phase 4 — shipped) over the frozen
   `PENDING_REPLACE → NEW` edge, exposed via `PATCH /orders/{client_order_id}`. The router
   branches on the capability row's `amend` field; native amends return the **same** cid,
   cancel_replace returns the **replacement** cid.
5. **Durable state + reconciliation.** Persist the lifecycle to `execution.orders` /
   `.fills` / append-only `.order_events` before anything reaches a venue; a reconciliation
   loop repairs submit/ack drift against broker truth.
6. **Order-submission endpoints are private/owner-mode** — public mode answers only health,
   capabilities, and reads. Raw broker payloads never cross the public boundary.

## Hard rules — inherited from the umbrella

1. **Always `uv run`** — never bare `python` / `pip` / `poetry` / `conda`.
2. **Async-first I/O** — all HTTP via `httpx.AsyncClient`. `requests` is forbidden in `src/`.
3. **Pydantic at boundaries** — module/external I/O goes through Pydantic models, never raw
   dicts.
4. **Monetary values are `Decimal`, never `float`,** at boundaries; serialise as strings on
   the wire. Prices are `numeric(18,6)` in the DB.
5. **Timezone:** store UTC, display `Asia/Bangkok`.
6. **No secrets in repo.** All config via env + `pydantic-settings`, prefix `EXECUTION_ENGINE_*`.
7. **Ingestion/submission is idempotent** (see service rule 2).
8. **`docs/plans/` is git-tracked.** The roadmap is part of the product — never gitignore it.

## Coding conventions worth knowing up front

- `from __future__ import annotations` at the top of every `src/` module.
- Module-local exceptions in each subpackage's `errors.py`, inheriting a shared base. Never
  `raise Exception(...)` or `except Exception: pass`.
- `logger = logging.getLogger(__name__)` — never `print` in `src/`; `%`-formatting in logs.
  **Never log a PIN, token, account number, or order payload secret.**
- File-size target ≤ 400 lines; functions ≤ ~50 lines.
- Tests mirror the source layout under `tests/`; `asyncio_mode = "auto"`. Internal imports use
  the `src.quant_execution_engine.…` prefix (matches `pythonpath = ["."]`).

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`,
`test:`, `chore:`, `refactor:`. Keep scope tight (`feat(adapters): Liberator place_order map`).

## Documentation

The documentation hub is [`docs/README.md`](docs/README.md) (Phase 7, tvkit-ref style). Every
endpoint, env var, and state transition is documented with a real example; `live` is documented as
gated; no secrets (SecretStr examples use `<your-value-here>`).

### `docs/` — hub + architecture

| File | Summary |
|------|---------|
| [`docs/README.md`](docs/README.md) | Documentation hub — one-line links to every sub-doc + conventions + cross-repo refs |
| [`docs/overview.md`](docs/overview.md) | One-paragraph service overview + pointers |
| [`docs/architecture/overview.md`](docs/architecture/overview.md) | Topology, the two planes (D1), gateway-proxy position, sole-credential owner, safety ladder, kill-switch, public mode |
| [`docs/architecture/state-machine.md`](docs/architecture/state-machine.md) | The frozen 9-state / 13-edge machine, terminal vs in-flight, append-only audit, idempotency, reconciliation window |
| [`docs/architecture/adapters.md`](docs/architecture/adapters.md) | `BrokerAdapter` interface, the full capability matrix, the two structural consequences, Sim/Liberator/Settrade notes |
| [`docs/architecture/security-boundary.md`](docs/architecture/security-boundary.md) | Credential ownership, public vs owner mode, PTRM gate + price-band, kill-switch, logging redaction |

### `docs/api/` — endpoint reference (each with a real curl example)

| File | Endpoint |
|------|----------|
| [`docs/api/health.md`](docs/api/health.md) | `GET /health` |
| [`docs/api/orders-submit.md`](docs/api/orders-submit.md) | `POST /orders` |
| [`docs/api/orders-get.md`](docs/api/orders-get.md) | `GET /orders/{client_order_id}` |
| [`docs/api/orders-cancel.md`](docs/api/orders-cancel.md) | `DELETE /orders/{client_order_id}` |
| [`docs/api/orders-amend.md`](docs/api/orders-amend.md) | `PATCH /orders/{client_order_id}` (native vs cancel+replace) |
| [`docs/api/orders-stream.md`](docs/api/orders-stream.md) | `GET /orders/stream` (SSE) |
| [`docs/api/capabilities.md`](docs/api/capabilities.md) | `GET /capabilities` |
| [`docs/api/order-book.md`](docs/api/order-book.md) | `GET /order-book/{symbol}[/stream]` |
| [`docs/api/admin.md`](docs/api/admin.md) | `/admin/kill-switch*` + `/admin/orders/{cid}/audit` + `/admin/audit/export` |

### `docs/operations/` + `docs/data/`

| File | Summary |
|------|---------|
| [`docs/operations/bring-up.md`](docs/operations/bring-up.md) | Three compose configs, schema prerequisite, health, tear-down, fresh-clone gotcha |
| [`docs/operations/configuration.md`](docs/operations/configuration.md) | Every `EXECUTION_ENGINE_*` env var — type / default / effect / SecretStr |
| [`docs/operations/kill-switch.md`](docs/operations/kill-switch.md) | Engage/disengage, the stage-flip rule, the breaker relationship |
| [`docs/operations/troubleshooting.md`](docs/operations/troubleshooting.md) | Breaker tripped, stuck pendings, burst guard, DB/Redis down, gateway 5xx |
| [`docs/data/execution-schema.md`](docs/data/execution-schema.md) | `db_execution` — `orders`/`fills`/`order_events`, triggers, indexes, grants |
| [`docs/data/state-machine-transitions.md`](docs/data/state-machine-transitions.md) | The verified 13-edge legal-transition table |

### `.claude/knowledge/`

| File | Summary |
|------|---------|
| [`.claude/knowledge/architecture.md`](.claude/knowledge/architecture.md) | Service architecture notes |
| [`.claude/knowledge/broker-research-liberator.md`](.claude/knowledge/broker-research-liberator.md) | Liberator API research |
| [`.claude/knowledge/broker-research-settrade.md`](.claude/knowledge/broker-research-settrade.md) | Settrade API research + per-market apps |
| [`.claude/knowledge/capability-matrix.md`](.claude/knowledge/capability-matrix.md) | The canonical cell-level capability matrix |
| [`.claude/knowledge/coding-standards.md`](.claude/knowledge/coding-standards.md) | Python conventions |
| [`.claude/knowledge/commands.md`](.claude/knowledge/commands.md) | CLI command reference |
| [`.claude/knowledge/decision-log.md`](.claude/knowledge/decision-log.md) | D-series / E-series decisions |
| [`.claude/knowledge/deployment.md`](.claude/knowledge/deployment.md) | **(Phase 7)** Compose topology, env-load order, fresh-clone |
| [`.claude/knowledge/normalized-order-contract.md`](.claude/knowledge/normalized-order-contract.md) | The frozen `NormalizedOrder` contract |
| [`.claude/knowledge/order-book-service.md`](.claude/knowledge/order-book-service.md) | Phase 5 dual-provider order book |
| [`.claude/knowledge/order-flow.md`](.claude/knowledge/order-flow.md) | **(Phase 7)** End-to-end order path (verified pipeline) |
| [`.claude/knowledge/order-state-machine.md`](.claude/knowledge/order-state-machine.md) | The frozen state machine |
| [`.claude/knowledge/order-update-stream.md`](.claude/knowledge/order-update-stream.md) | Phase 5 SSE order-update stream |
| [`.claude/knowledge/project-skill.md`](.claude/knowledge/project-skill.md) | Project skill definition |
| [`.claude/knowledge/stack-decisions.md`](.claude/knowledge/stack-decisions.md) | Technology stack decisions |

### `.claude/playbooks/`

| File | Summary |
|------|---------|
| [`.claude/playbooks/development-workflow.md`](.claude/playbooks/development-workflow.md) | **(Phase 7)** Quality gate, branch naming, bring-up, respx-mocked tests, the Python 3.11 CI gotcha |
| [`.claude/playbooks/order-routing-safety.md`](.claude/playbooks/order-routing-safety.md) | The irreversible-action checklist + venue runbooks (Liberator/Settrade, kill-switch, audit) |
| [`.claude/playbooks/feature-development.md`](.claude/playbooks/feature-development.md) | Feature dev workflow |
| [`.claude/playbooks/bugfix-workflow.md`](.claude/playbooks/bugfix-workflow.md) | Bug investigation & fix |
| [`.claude/playbooks/code-review.md`](.claude/playbooks/code-review.md) | Code-review checklist |
| [`.claude/playbooks/dependency-upgrade.md`](.claude/playbooks/dependency-upgrade.md) | Dependency upgrade |
| [`.claude/playbooks/release-checklist.md`](.claude/playbooks/release-checklist.md) | Release checklist |

The umbrella operator runbook is [`../.claude/playbooks/execution-engine-runbook.md`](../.claude/playbooks/execution-engine-runbook.md).

## Where to look next

- **Documentation hub (every endpoint, env var, state transition):** [`docs/README.md`](docs/README.md)
- **Roadmap (source of truth for what to build next):** [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md)
- **Architecture ADR (Phase-0 gate, D1–D13):** [`../.claude/knowledge/feature-execution-engine.md`](../.claude/knowledge/feature-execution-engine.md)
- **Broker research (cited):** [`.claude/knowledge/broker-research-liberator.md`](.claude/knowledge/broker-research-liberator.md),
  [`.claude/knowledge/broker-research-settrade.md`](.claude/knowledge/broker-research-settrade.md)
- **Capability matrix / contract / state machine:** [`.claude/knowledge/capability-matrix.md`](.claude/knowledge/capability-matrix.md),
  [`.claude/knowledge/normalized-order-contract.md`](.claude/knowledge/normalized-order-contract.md),
  [`.claude/knowledge/order-state-machine.md`](.claude/knowledge/order-state-machine.md)
- **Decision log:** [`.claude/knowledge/decision-log.md`](.claude/knowledge/decision-log.md)
- **Order-update stream schema/contract (Phase 5):** [`.claude/knowledge/order-update-stream.md`](.claude/knowledge/order-update-stream.md)
- **Order book service architecture (Phase 5):** [`.claude/knowledge/order-book-service.md`](.claude/knowledge/order-book-service.md)
- **Order-routing safety playbook:** [`.claude/playbooks/order-routing-safety.md`](.claude/playbooks/order-routing-safety.md)
- **Pattern precedent (standalone credential-owner engine):** `../quant-marketdata-engine/CLAUDE.md`
- **Umbrella system map:** [`../CLAUDE.md`](../CLAUDE.md)

# quant-execution-engine — ROADMAP

> **⚠️ Updated 2026-07-18 — broker-023 / `settrade_v2` (the Settrade Open API) REMOVED (Option B).**
> Real execution routing is now **Sim + Liberator + Streaming Pro** (Streaming Pro = the self-built
> `settrade-streaming-api` retail bridge; FINANSIA-024 HOME / SBITO-033 AWS). The `SettradeAdapter`,
> its order-book provider, config, the `settrade-v2` dependency, and the Phase-4 plan docs are gone.
> **The Phase-4 / Phase-4.1 sections and the Phase-5/6 Settrade sub-parts below are SUPERSEDED
> period records — not rewritten.** See
> [`../../.claude/knowledge/decision-log.md`](../../.claude/knowledge/decision-log.md) → the
> 2026-07-18 removal entry. Native amend was Settrade-only among real brokers, so the engine loses
> real-broker native amend (Liberator + Streaming Pro use `cancel_replace`; only `sim` is native).
> Terminology: "Streaming Pro" / `streaming_pro` = KEEP; "Settrade" / `settrade_v2` = REMOVED.

The **Execution engine**: a single standalone service (host `:8400`, container `:8000`,
gateway-proxied under `/api/v2/engines/execution/*`) that is the **only** thing that sends
orders to brokers. Every strategy submits one canonical `NormalizedOrder`; the engine routes
it to a broker adapter (Liberator, Streaming Pro) or the `SimAdapter`. Strategies **never** speak a
broker's native order API and **never** hold a broker credential.

> This is the **per-service realisation** of the cross-cutting
> [`feature-execution-engine`](../../../plans/feature-execution-engine/ROADMAP.md) (umbrella
> Decision Log **D1–D13**). The umbrella ADR is the Phase-0 gate:
> [`.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md).
> Development is **dependency-ordered** — each phase must be complete and validated before the
> next begins. The goal is **safe, durable, idempotent order routing**, not a feature count.
> Order routing is **irreversible and outward-facing**: a dropped market-data tick is a
> resubscribe, a duplicated buy order is a real loss. Safety is Phase-2 wiring, never a
> Phase-6 afterthought.

**Status: Phases 0–8 complete — Phase 8 (`StreamingProAdapter`, the THIRD real broker) shipped
2026-06-16: composes the `settrade-streaming-api` retail bridge over plain httpx (mirrors Liberator),
conservative `(MARKET,LIMIT)×DAY` SET+TFEX cells, `cancel_replace` amend, no PIN (bridge-owned); 1034
tests, 95.26% cov; `live` gated, micro_live soak operator-driven. (Phase 5/5.1: 2026-06-12; Phase 6
hardening + Phase 7 docs: 2026-06-13.) ADR accepted; order store live;
engine core + SimAdapter + gateway proxy live; `LiberatorAdapter` + `SettradeAdapter` (both real
venues) live; the normalized order-update stream out + the dual-provider order book service live;
both strategies run the end-to-end sim trade loop behind `*_EXECUTION_MODE` flags; the failure
paths hardened under fault injection (Phase 6); Phase 7 (documentation) Proposed.** The full order
path
runs end-to-end through the gateway: submit/dedupe/fills/cancel/**native-amend** over the
durable store, PTRM + kill-switch + stage ladder wired from the first path. Two real brokers
route the **same** `NormalizedOrder` by `broker`/account (Liberator cancel+replace amend;
Settrade native amend via `PATCH /orders/{cid}`). **Phase 5 (engine side)** shipped the
normalized **order-update stream out** (`GET /orders/stream`, D12 realised), the in-engine
**dual-provider order book** (Settrade realtime + Liberator WebSocket) feeding `SimAdapter`
live fill prices, and `X-Strategy-Id` identity — all additive and default-off; `live` gating,
the kill-switch, PTRM, and the frozen contracts are **unchanged**. **`live` stays gated — no
real-money default**; `micro_live` is the highest rung the adapters exercise, operator-driven.
The strategy-side scope (the `*_EXECUTION_MODE` flags + the end-to-end sim trade loop) split to
**Phase 5.1** by operator decision and **shipped the same day** (2026-06-12): csm-set PR #16,
tfex PR #18, gateway PR #24 (`X-Strategy-Id` forwarding), live-verified end-to-end in sim.
**Phase 6 (2026-06-13)** hardened the failure paths under fault injection — per-account caps +
advisory price-band + unified default-on duplicate-burst guard, kill-switch admin-trip hardening +
a 5-order fault test, an idempotency soak + reconciliation drift suite, per-adapter rate-limit
token buckets + an `EventHub` §H stress test, and a structured audit read + NDJSON export — all
additive, `live` still gated, no frozen-contract or infra-db schema change. Next: **Phase 7**
(documentation, tvkit-ref style).

---

## Status Legend

| Symbol | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress |
| `[x]` | Complete |
| `[-]` | Skipped / deferred |

---

## Design principles (from the umbrella ADR, D1–D13)

1. **Two planes, never merged (D1).** Order execution (low-volume, real-money, idempotent,
   durable) and market-data streaming (high-volume, lossy-tolerant) have opposite reliability
   profiles. This service is the **execution plane only** — streaming stays in
   `order-book-infrastructure` + `quant-marketdata-engine`. We may *read* their feeds for
   price-band risk checks; we never own them.
2. **One router owns every credential (D2).** Only this service holds broker order-routing
   sessions (Liberator OTP/PIN, Settrade OAuth). The gateway proxies and holds **no**
   credential; no strategy or host submits directly.
3. **Idempotency is core, not polish (D5).** Every order carries a client-generated
   `client_order_id`; the engine dedupes and persists a lifecycle **before** anything reaches
   a venue. Re-submitting the same id is safe — the order-execution analogue of the ingestion
   contract's `INSERT … ON CONFLICT`.
4. **Adapters declare capabilities; the router enforces them (D7).** No lowest-common-
   denominator pretending, no silent venue failures. An unsupported
   `(broker, market, order_type, tif)` is rejected up front with a typed error.
5. **Normalize the contract, not the auth (D10).** The order schema + status enum are shared;
   each adapter's session/login (Liberator OTP vs Settrade OAuth) stays irreducibly inside
   that adapter.
6. **Sim-first, safety-gated (D11).** A `SimAdapter`, a pre-trade risk gate, and a global
   kill-switch exist from the first end-to-end wiring; real-money adapters land only after the
   contract is proven against sim. Monetary fields are `Decimal` at the boundary.

---

## Safety ladder — `EXECUTION_ENGINE_STAGE` (the single most important rule)

> **No order reaches a real broker until the stage is explicitly raised to `live` AND the
> global kill-switch is disengaged.** The stage is the execution analogue of the tfex capital
> ladder and the market-data engine's `mirror|engine` cutover flag: default to the safest
> rung, gate the dangerous one, make the cutover deliberate and reversible.

| Stage | Default? | Behaviour | Routes to |
|---|---|---|---|
| **`sim`** | ✅ default | Deterministic paper fills, no real money, no broker session. The contract, state machine, idempotency, and risk gate all run for real. | `SimAdapter` |
| **`paper`** | — | Live broker **read** (account/positions/quotes) for realism, but order submission is intercepted and simulated — never sent. | `SimAdapter` (+ broker read) |
| **`micro_live`** | — | Real orders at the **smallest venue size only**, behind the kill-switch + per-account caps. The first rung that can lose money. | real adapter, size-capped |
| **`live`** | — (gated) | Full real-money routing. Requires owner mode (`EXECUTION_ENGINE_PUBLIC_MODE=false`), the stage set to `live`, the kill-switch disengaged, and per-account caps configured. | real adapter |

- The **global kill-switch** (`EXECUTION_ENGINE_KILL_SWITCH_ENGAGED=true`, or the runtime
  admin trip) overrides **every** stage: reject all new submits with a typed error **and
  mass-cancel open orders** (flatten-and-halt). It is checked **first** in the submit path
  (hard rule, mirrors tfex hard rule #8).
- **Public mode** (`EXECUTION_ENGINE_PUBLIC_MODE=true`, the Docker default) disables all
  order-submission endpoints regardless of stage — only `/health`, capability matrix, and
  read endpoints answer.

---

## Broker capability matrix

> **Updated 2026-07-18 — broker-023 / `settrade_v2` (Settrade Open API) removed.** The real
> brokers are now **Liberator** + **Streaming Pro** (the self-built `settrade-streaming-api` retail
> bridge). The former Settrade Open-API column is gone. The canonical cell-level matrix lives in
> [`.claude/knowledge/capability-matrix.md`](../../.claude/knowledge/capability-matrix.md); the
> Phase-4/4.1 Settrade sections below are SUPERSEDED period records. Terminology: "Streaming Pro" /
> `streaming_pro` = KEPT bridge; "Settrade" / `settrade_v2` = REMOVED Open API — never conflate.

Mapped onto one `NormalizedOrder`. Liberator cells are sourced from the existing
`liberator-trading-api` service models (validated against the live adapter since Phase 3);
Streaming Pro cells are the conservative Phase-8 live-verified set (expand as verified).

| Capability | **Liberator** (SET / TFEX) | **Streaming Pro** (SET + TFEX) | **Sim** |
|---|---|---|---|
| Auth model | OTP/2FA + SMS-webhook refresh; Redis-backed token; per-order **PIN** | bridge-owned login/OTP/session; engine holds only the bridge api-key — **no PIN** (the bridge stamps it) | none |
| Markets | **SET** (equity) + **TFEX** (derivatives) | **SET** (`fis`) + **TFEX** (`seosd`) via the retail bridge | any (configurable) |
| Place order | `POST /order/place/{set,tfex}` | bridge order REST (plain httpx) | in-proc |
| `side` | SET `Buy/Sell`; TFEX `Long/Short` | SET `Buy/Sell`; TFEX `Long/Short` | both |
| `position_effect` | TFEX `Open/Close/Auto`; SET n/a | TFEX `Open/Close`; SET n/a | both |
| MARKET / LIMIT | ✅ (`Market`/`Limit`) | ✅ (only these live-verified) | ✅ |
| STOP / STOP_LIMIT | TFEX ✅ (`priceType=Stop` + `stopSymbol`/`stopPrice`/`stopCondition`); SET ✗ | ✗ (conservative v1) | ✅ |
| ICEBERG (display qty) | ✅ `icebergVol` (SET + TFEX) | ✗ (conservative v1) | ✅ |
| ATO / ATC | SET ✅ (`priceType=ATO/ATC`); TFEX ✗ | ✗ (conservative v1) | ✅ |
| MTL / MP (market-to-limit) | SET ✅ (`priceType=MP`) | ✗ (conservative v1) | ✅ |
| TIF | `Day/GTC/IOC/FOK` | `Day` only (conservative v1) | all |
| Amend | ✗ **no amend route** → adapter does **cancel + replace** | ✗ bridge `/order/change` 501 → **cancel + replace** | ✅ native |
| Cancel | `POST /order/cancelled/{set,tfex}` by `orderNo` list (≤50) + PIN | bridge cancel (bridge-stamped PIN) | ✅ |
| Query (reconcile) | `GET /orders`, `/orders/{account_no}`, `/orders/summary` (status, matched/balance/cancelled, reject_code, can_cancel) | bridge `fetch_venue_orders` read | in-proc state |
| Order-update stream | indirect: `POST /ws-ticket` issues a venue WS ticket (no normalized push) | reconciler-fed engine-normalized stream (`GET /orders/stream`, SSE) | synthetic events |
| Client idempotency key | ✗ (broker `orderNo` only) | ✗ (bridge order id only) | n/a |

**Two findings that shape the design:**
- **Neither real broker accepts a client idempotency key.** The engine therefore owns the
  `client_order_id` ↔ `broker_order_id` mapping in its durable store, and dedupe happens in
  the engine **before** routing (D5). This is the crux of exactly-once-ish submission.
- **Amend is cancel+replace for every real broker.** The `BrokerAdapter.amend()` contract is
  uniform; `LiberatorAdapter.amend` and `StreamingProAdapter.amend` are both implemented as
  **cancel-then-replace** (declared in their capability sets). Native in-place amend was
  Settrade-only and left with broker-023 (2026-07-18); among current brokers only `sim` is native.

---

## `NormalizedOrder` / `NormalizedOrderResult` contract (frozen in Phase 0; realised in Phase 2)

```text
NormalizedOrder
  client_order_id : str          # client-generated idempotency key (UUID/ULID)
  broker          : "sim" | "liberator" | "streaming_pro"
  account         : str          # broker account ref (never logged in full)
  market          : "SET" | "TFEX"
  symbol          : str          # venue symbol, e.g. "PTT", "S50H26"
  side            : "BUY" | "SELL"
  order_type      : "MARKET" | "LIMIT" | "STOP" | "STOP_LIMIT"
                  | "ICEBERG" | "MTL" | "ATO" | "ATC"
  price           : Decimal?     # required for LIMIT / STOP_LIMIT — Decimal-as-string on wire
  stop_price      : Decimal?     # required for STOP / STOP_LIMIT
  quantity        : int          # contracts (TFEX) or shares (SET)
  display_qty     : int?         # iceberg display size
  tif             : "DAY" | "IOC" | "FOK" | "GTC"
  position_effect : "OPEN" | "CLOSE" | None     # TFEX only; None for SET cash
  metadata        : dict         # opaque strategy tags (never sent to venue)

NormalizedOrderResult
  client_order_id : str
  broker_order_id : str?         # venue id once acked
  broker          : str
  status          : NormalizedStatus
  filled_qty      : int
  remaining_qty   : int
  avg_fill_price  : Decimal?
  reject_reason   : str?         # mapped from broker reject_code/err_msg
  created_at / updated_at : datetime   # UTC store, Asia/Bangkok display
  raw             : dict?        # private-only, never crosses the public boundary

NormalizedStatus  =  NEW | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | EXPIRED
```

**Side / position mapping** (declared per adapter, enforced by the router):
`BUY`→ Liberator SET `Buy` / TFEX `Long` (Streaming Pro maps the same); `SELL`→ `Sell` / `Short`.
`Decimal`-as-string on the wire; `int` quantities; UTC timestamps. No `float` at any money
boundary (umbrella rule).

### Order state machine

```text
            submit (deduped on client_order_id)
PENDING_NEW ───────────────► NEW ──fill(partial)──► PARTIALLY_FILLED ──fill(rest)──► FILLED
   │  │                       │  │                          │
   │  └── reject ────────────►│  └── cancel ───► CANCELLED ◄┘ (cancel)
   │                          └── expire ─────► EXPIRED
   └── reject (pre-route) ───► REJECTED
```

- Local states `PENDING_NEW` / `PENDING_CANCEL` / `PENDING_REPLACE` cover the network gap
  between submit and ack (the reconciliation window).
- Terminal: `FILLED`, `CANCELLED`, `REJECTED`, `EXPIRED`. Illegal transitions are rejected by
  an app-level guard (and a DB constraint where practical). Every transition appends an
  immutable row to `execution.order_events` (audit trail).

---

## Per-service impact (which repo each phase touches)

| Repo | Effect |
|---|---|
| **`quant-execution-engine`** (this repo) | The engine: `BrokerAdapter` interface, `NormalizedOrder` contract + capability matrix, order state machine + idempotency, reconciliation loop, pre-trade risk gate + kill-switch, order-update stream, `SimAdapter`, `LiberatorAdapter`, `StreamingProAdapter`. |
| **`quant-infra-db`** | New `execution` schema (Phase 1): `orders` / `fills` / append-only `order_events`. Its own PR. |
| **`quant-api-gateway`** | Proxy route `/api/v2/engines/execution/*` → `:8400` + register the 5th engine (Phase 2). Its own PR. **No broker credential.** |
| **`liberator-trading-api`** (existing) | Becomes a `LiberatorAdapter` **target** over HTTP (Phase 3), **vendored as a git submodule** under `third_party/liberator-trading-api/` and bundled into this repo's owner-mode bring-up (`docker-compose.liberator.yml`) as an **internal-only** upstream (no host port). May add a normalized order-update hook. No strategy calls it directly anymore. |
| **`strategies/csm-set`, `strategies/tfex-s50-multi-tf-swing`** | Gain an execution path behind `*_EXECUTION_MODE = off\|sim\|live` (**Phase 5.1**). Submit `NormalizedOrder`s; consume `GET /orders/stream`; never speak a broker API. |

---

## Phases

> Each phase names its **quality gate**: `ruff` + `mypy --strict` + `pytest` with **≥90%
> coverage on `adapters/` + the order state machine** (`--cov-fail-under=90`), matching CI.
> Cross-repo phases land in the named sub-repo's **own** PR. Real-money routing is gated
> behind sim + kill-switch; no strategy sends live orders before the Phase-5 cutover.

### Phase 0 — Design & ADR gate 🧭
**Status:** `[x]` **Complete (2026-06-10).** **Repo:** umbrella `.claude/knowledge/` + this
repo's docs. **Shipped:** the umbrella ADR promoted to **ACCEPTED** — D1–D13 confirmed as
drafted; the `NormalizedOrder` contract + `BrokerAdapter` interface + order state machine +
capability-matrix shape frozen; every open question below pinned (ADR §A–§G, most notably the
**at-least-once + dedupe + reconcile** delivery guarantee and the
`client_order_id ↔ broker_order_id` mapping rule); this repo's knowledge seed confirmed &
frozen against the ADR. Phase plan: [`phase0-design-adr-gate.md`](phase0-design-adr-gate.md).
**Phase 1 unblocked.**

- **Objective:** freeze the contracts so every later phase builds against a fixed target.
- **Scope (ships):** promote the umbrella ADR stub
  [`feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md) to a
  full ADR — confirm Decision Log **D1–D13**, pin the `NormalizedOrder` fields + status enum,
  the `BrokerAdapter` interface signature, the order state machine (states + legal
  transitions), and the capability-matrix shape. Register the service in the umbrella
  `CLAUDE.md` (repo table, network contract, engine catalog, bring-up, health check). Author
  this repo's `.claude/knowledge/` (capability matrix, contract, state machine, broker
  research, decision log). *(This scaffold PR delivers the repo + this ROADMAP + the knowledge
  seed; the ADR confirmation is the gate to start Phase 1.)*
- **Non-goals:** no code that touches a broker; no schema; no routes beyond `/health`.
- **Acceptance:** ADR merged with D1–D13 accepted; contract + state machine + capability
  matrix agreed; service registered in the umbrella; scaffold gate green.
- **Cross-refs:** umbrella ROADMAP Phase 0; ADR; this repo `.claude/knowledge/*`.

### Phase 1 — `quant-infra-db` `execution` order store 🗄️
**Status:** `[x]` **Complete (2026-06-10).** **Repo:** `quant-infra-db`
([PR #11](https://github.com/lumduan/quant-infra-db/pull/11), `12_schema_execution.sql`).
**Shipped:** `db_execution` (dedicated DB, plain tables — no TimescaleDB; FK targets cannot
be hypertables) with `execution.orders` (**PK `client_order_id`** = the idempotency
constraint; frozen enums as CHECKs; `numeric(18,6)` prices; §B reconciliation index
`(account, symbol, side, quantity, created_at)` + partial `broker_order_id` index),
`execution.fills` (`UNIQUE (client_order_id, broker_fill_id)` dedupes at-least-once
delivery), and append-only `execution.order_events`; DB triggers enforce the entry state +
**exactly the 13 frozen state-machine edges** (terminals immutable) and auto-append one
audit row per transition with `broker_order_id` snapshotted atomically on ack (§B).
Live-applied twice (idempotent) to `quant-postgres` on `quant-network`; 14 infra tests +
infra-db gate green. Phase plan:
[`phase1-execution-order-store.md`](phase1-execution-order-store.md). **Phase 2 unblocked.**

- **Objective:** a durable, idempotent, auditable order store that survives restarts and
  powers reconciliation.
- **Scope (ships):** `execution.orders` (PK on `client_order_id`; `broker`, `broker_order_id`,
  `account`, `symbol`, `market`, `side`, `order_type`, `price`/`stop_price` `numeric(18,6)`,
  `quantity`/`display_qty`, `tif`, `position_effect`, `status`, timestamps); `execution.fills`
  (partial/total executions, `numeric` price/qty); append-only `execution.order_events` (every
  transition); the idempotency constraint on `client_order_id`; a legal-transition guard
  (app-level and/or trigger).
- **Non-goals:** no engine code; no adapters.
- **Acceptance:** schema applies idempotently on `quant-network`; a duplicate `client_order_id`
  is rejected; illegal transitions are rejected; `db_execution` reachable as `quant-postgres`.
- **Quality gate:** infra-db's gate (migration applies clean; idempotency + constraint tests).
- **Cross-refs:** umbrella ROADMAP Phase 1; marketdata-engine Phase 1 (`market_data` schema)
  as the schema-PR precedent.

### Phase 2 — Engine core + gateway proxy + `SimAdapter` 🚦
**Status:** `[x]` **Complete (2026-06-10).** **Repos:** this repo + `quant-api-gateway` +
`quant-infra-db` (own PRs). **Shipped:** the full sim order path over the Phase-1 store —
frozen `NormalizedOrder`/`Result` contracts (+ additive `engine_state`), pure 13-edge state
machine, `OrderRouter` (kill-switch-first; dedupe ⇒ prior result; single-flight lock with
PK backstop; §B-atomic ack; synchronous deterministic fills; IOC cancel walk), PTRM caps +
rate/burst throttles, runtime kill-switch with best-effort mass-cancel, circuit-breaker
scaffolding, deterministic `SimAdapter` (`sim_fills`/`sim_reject` control channel),
`POST/GET/DELETE /orders` + `/capabilities` + owner-mode `/admin/kill-switch*`; gateway
proxy `/api/v2/engines/execution/*` (typed envelopes pass through; no credential);
infra-db: engine-registry row, durable `reject_reason`, least-privilege `quant` grants.
Live acceptance passed end-to-end through the gateway (dedupe, partial fills, audit rows,
typed rejects, kill-switch mass-cancel). 104 engine + 349 gateway tests, mypy strict,
~99%/90.7% cov. **No real-money path exists.** Phase plan:
[`phase2-engine-core-simadapter.md`](phase2-engine-core-simadapter.md). **Phase 3 unblocked.**

- **Objective:** prove the full lifecycle end-to-end against sim, with safety wired from the
  first path.
- **Scope (ships):** FastAPI surface (`POST /orders`, `GET /orders/{client_order_id}`, cancel,
  `GET /capabilities`); the `BrokerAdapter` interface
  (`place`/`cancel`/`amend`/`get_open_orders`/`get_positions`/`get_account`/`capabilities`);
  `NormalizedOrder` Pydantic models; the order state machine over the Phase-1 store; **idempotent
  submit deduped on `client_order_id`**; the **pre-trade risk gate (PTRM caps: max order
  value/qty, per-second order rate limit) + global kill-switch (reject new + mass-cancel
  open)**; per-adapter session **circuit-breaker scaffolding** (heartbeats land with the real
  adapters, Phases 3/4); `SimAdapter` (deterministic paper fills); the
  `EXECUTION_ENGINE_STAGE` ladder (default `sim`).
  **`quant-api-gateway`** (own PR): proxy `/api/v2/engines/execution/*` → `:8400`, register the
  **Execution** engine (`EXTERNAL`) in the catalog, auth-gated, **no broker credential**.
- **Non-goals:** no real broker; no reconciliation against a venue (sim is in-proc).
- **Acceptance:** a `NormalizedOrder` POSTed through the gateway routes to `SimAdapter`,
  persists a full lifecycle, dedupes on resend (same `client_order_id` ⇒ prior ack), and
  rejects unsupported / over-cap / kill-switched / wrong-stage orders with typed errors. **No
  real-money path exists.**
- **Quality gate:** ruff + mypy strict + pytest ≥90% on `adapters/` (incl. `SimAdapter`) + the
  state machine; gateway gate green.
- **Cross-refs:** umbrella ROADMAP Phase 2; marketdata-engine Phase 2 (service build + proxy).

### Phase 3 — `LiberatorAdapter` (first real broker) 🔌
**Status:** `[x]` **Complete (2026-06-11).** **Repos:** this repo + `liberator-trading-api`
(auth hardening, dual-commit). **Shipped:** `adapters/liberator/` — pure SET/TFEX payload
mapping (Buy/Sell vs Long/Short, MTL→MP, ICEBERG→Limit+icebergVol, TFEX stop fields),
redacting httpx transport (api-key via `SecretStr`; PIN/account never logged — tested),
`LiberatorAdapter` (place/cancel/amend-declaration/reads/heartbeat; venue rejects carry
`errorCode`/`errMsg` text, never swallowed), **reconciliation loop v1** (cumulative-matched
delta fills with deterministic ids, §B lost-ack fuzzy match ±5 s, bounded 60 s
`ack_lost_unmatched`, venue terminals onto legal edges only), **heartbeat + circuit breaker**
(trip ⇒ typed `broker_circuit_open` + mass-cancel attempted; state on `/health` +
`/capabilities`), stage matrix (`paper` intercepts placement to sim with the session live;
`micro_live` routes real; `live` still gated), router-level cancel+replace `amend()` with a
new `client_order_id` (no HTTP route until Phase 4), `EXECUTION_ENGINE_LIBERATOR_*` settings,
capability rows `adapter_installed=true`. Upstream hardening (own repo, pinned): shared
timing-safe `verify_api_key` + UTC health timestamps. 343 tests, mypy strict, ~97 % cov.
**Real micro_live venue validation is operator-driven** (OTP login) per the safety playbook's
Liberator runbook. Phase plan: [`phase3-liberator-adapter.md`](phase3-liberator-adapter.md).
**Phase 4 unblocked.**

- **Objective:** route a real order to a real venue, end-to-end, idempotently.
- **Scope (ships):** `LiberatorAdapter` composing `liberator-trading-api` over HTTP (D9 — it
  does **not** re-implement Liberator): map `NormalizedOrder` → `POST /order/place/{set,tfex}`
  (`side`, `position`, `priceType`, `validityType`, `icebergVol`, stop fields, PIN); map
  Liberator status / `reject_code` → the normalized status enum; **amend = cancel + replace**
  (Liberator has no amend route — declared in its capability set); reconciliation loop v1
  against `GET /orders` to repair submit/ack drift; **proactive session heartbeat** (~30 s
  low-impact read) tripping the circuit breaker on consecutive failures (ADR §G). Validate in
  **`paper` / `micro_live` smallest size** behind the kill-switch.
- **Deployment (bundled upstream).** `liberator-trading-api` is vendored as a **git submodule**
  at `third_party/liberator-trading-api/` (its own repo, pinned commit, untouched — D9 says the
  adapter *composes* it, never re-implements it). It is an **internal piece of the execution
  plane, not a platform peer**: **no host port**, **not** registered in the umbrella network
  table, and **no independent bring-up** — its lifecycle is bundled with owner mode via the
  `docker-compose.liberator.yml` overlay:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.private.yml -f docker-compose.liberator.yml up -d
  ```
  The public/sim default (`docker compose up`) stays **broker-free** — liberator only joins when
  the overlay is layered. Wiring notes: liberator listens on internal port **8200** (its
  `config/system.yaml api.port`), so the adapter target is `http://liberator-trading-api:8200/api/v1`;
  it gets its **own** Redis sidecar (`liberator-redis`, distinct from `execution-redis`); and
  because its settings loader merges YAML **over** env vars, the container's Redis host/port are
  pinned via a mounted `docker/liberator/system.yaml` (env `REDIS_HOST` would be ignored). The
  submodule ships only stub `Dockerfile`/`compose`, so the image is built from this repo's
  `docker/liberator/Dockerfile` (Python ≥3.13). Broker creds live only in this repo's gitignored
  `.env`. Fresh checkouts need `git submodule update --init`.
- **Non-goals:** no Settrade; no streaming push yet (Phase 5); `live` stage stays gated.
- **Acceptance:** a normalized order reaches Liberator and the round-trip (ack → fills →
  status) reconciles; re-submission is idempotent; the capability matrix reflects Liberator's
  real SET/TFEX support; gate green.
- **Quality gate:** ruff + mypy strict + pytest ≥90% on `adapters/liberator*` + state machine
  (HTTP mocked; no live creds in CI).
- **Cross-refs:** umbrella ROADMAP Phase 3; `.claude/knowledge/broker-research-liberator.md`.

### Phase 4 — `SettradeAdapter` (second broker — proves the abstraction) 🔌

> **SUPERSEDED 2026-07-18 — period record.** The `SettradeAdapter` (broker-023 / `settrade_v2`,
> the Settrade Open API) was **removed** (Option B). This section is retained verbatim as the
> historical Phase-4 record and is not rewritten. Real routing is now Sim + Liberator + Streaming
> Pro — see the banner at the top of this file and the decision-log 2026-07-18 entry.
**Status:** `[x]` **Complete (2026-06-11).** **Repo:** this repo
(`feature/phase4-settrade-adapter`). **Shipped:** `adapters/settrade/` — the second real venue,
routing the **same** `NormalizedOrder` to either broker with **zero contract change**. Full
**SET equity** (`/api/seos/v3`) **+ TFEX derivatives** (`/api/seosd/v3`) — the old "no SET on
Settrade (derivatives first)" non-goal was **struck by operator decision**. OAuth session lives
inside the adapter (D10) over a **raw `httpx.AsyncClient`** (the sync `settrade-v2` SDK is
forbidden — `requests`-based + import-time side effects: writes `~/settradesdkv2_config.txt`,
NTP call, version-check HTTP): ECDSA P-256/SHA256 login signing, single-flight `ensure_token()`,
proactive refresh inside the 100 s margin, refresh-fail→fresh-login (the SDK's silent-ignore bug
not copied), one serial-guarded reactive-401 retry; creds/PIN/tokens/signature all `SecretStr`/
redacted (account rides the URL path → `redact_path` + httpx logger demoted to WARNING).
**Native amend** over the frozen `PENDING_REPLACE → NEW` edge — one atomic `replace_order`
(status+price+qty so the audit row snapshots amended values); venue amend-reject is a
NON-terminal restore + typed `AmendRejected` (409), `reject_reason` deliberately untouched
(order still live); kill-switch gates amends up front (asymmetry vs un-gated cancel), PTRM
re-check with NO exemption. New **`PATCH /orders/{client_order_id}`** route (native amends in
place / cancel_replace returns the replacement cid). Reconciler v1 mirrors Liberator (E18
watermark fills `broker_fill_id=f"{order_no}:{matched}"`, §B constants verbatim, GET-budget
skip) with a new **`replace_resolve`** action for stranded `PENDING_REPLACE`. Heartbeat =
**OAuth token-liveness probe** (Settrade has no health endpoint) → breaker trip ⇒
`broker_circuit_open` + mass-cancel; `/health` brokers dict carries `settrade` alongside
`liberator`. Stage matrix (`paper` intercepts placement to sim with the session live for reads;
`micro_live` routes `broker=settrade` real at PTRM cap; **`live` stays gated** — typed reject).
Rate limits observe-don't-throttle (GET vs POST+PATCH buckets; reconciler budget-skip).
Capability cells **pinned from `developer.settrade.com`** (replaced every `(confirm P4)` cell;
both SET + TFEX, `amend="native"`, `adapter_installed=True`). `EXECUTION_ENGINE_SETTRADE_*`
settings (creds/PIN `SecretStr`), `cryptography>=42` dep; **no compose overlay** (cloud API —
creds ride `docker-compose.private.yml`'s `env_file`). 687 tests passed, mypy strict, 96.14%
total coverage (settrade modules 93–100%). Phase plan:
[`phase4-settrade-adapter.md`](phase4-settrade-adapter.md). **Phase 5 unblocked.**

- **Objective:** add a second broker by writing **one adapter**, with zero contract change —
  the test that the abstraction is real.
- **Scope (shipped):** `SettradeAdapter` owning the Settrade Open API **OAuth** session inside
  the adapter (D10 — `app_id`/`app_secret`/`app_code`/`broker_id`/`pin` → token, proactive
  refresh, rate-limit aware) over a raw `httpx.AsyncClient` (not the sync SDK); map
  `NormalizedOrder` ↔ the SET + TFEX `POST .../orders` wire and the **native** amend
  (`PATCH .../change`) / cancel (`PATCH .../cancel`); map status/`rejectCode`; **pinned** the
  exact Settrade `priceType`/`validityType`/stop-condition enum sets (the former `(confirm P4)`
  cells) and declared the capability divergences from Liberator (native amend, SET + TFEX,
  distinct enum sets); OAuth token-liveness heartbeat + circuit breaker (no venue health route).
  The router rejects `(settrade, market, type, tif)` combos Settrade doesn't support
  **before** hitting the venue (SET stops, TFEX ATC, `Date`/`Auto`/NVDR/SESSION).
- **Non-goals (deferred):** streaming order-update push (Phase 5 — Settrade native MQTT
  `subscribe_{derivatives,equity}_order`); `MarketRep*`/`MarketData` SDK surfaces (D1 plane
  split); `_place_orders` private batch; NVDR (`trusteeIdType` pinned `Local`); `live` unlock.
- **Acceptance (met):** the **same** `NormalizedOrder` routes to either broker by `broker`/account
  with no contract change; capability divergences are enforced up front, not discovered at the
  venue; gate green.
- **Quality gate:** ruff + mypy strict + pytest ≥90% on `adapters/settrade*` + the native-amend
  router path (respx-mocked; no live creds in CI).
- **Cross-refs:** umbrella ROADMAP Phase 4; `.claude/knowledge/broker-research-settrade.md`
  (venue-docs scraping recipe + equity surface addendum + implemented-vs-researched).

### Phase 4.1 — Settrade per-market broker apps 🔌

> **SUPERSEDED 2026-07-18 — period record.** Removed with broker-023 / `settrade_v2` (Option B);
> retained verbatim as the historical Phase-4.1 record, not rewritten.
**Status:** `[x]` **Complete (2026-06-11).** **Repo:** this repo
(`feature/phase4.1-settrade-per-market-apps`). **Shipped:** a focused in-engine refactor —
`SettradeAdapter` now holds **one `SettradeClient` per market** (ctor `clients:
Mapping[Market, SettradeClient]`) behind the **unchanged** `NormalizedOrder` contract, so the
real broker **InnovestX (023)** — which splits the books across two OAuth apps (`ALGO_EQ` = SET
equity, `ALGO` = TFEX derivatives) — can route both legs of a stock-vs-futures spread
**concurrently**. Six new per-market settings
(`EXECUTION_ENGINE_SETTRADE_{EQUITY,DERIVATIVES}_APP_{ID,SECRET,CODE}`); per-market credentials
resolve **independently** with **partial-trio-fails-loud** (a PARTIAL per-market trio leaves the
market UNCONFIGURED with a boot WARNING naming the missing fields — **no silent fallback** to the
shared app); the shared `settrade_app_*` trio remains the single-app sandbox path; clients are
deduped by credentials value (the sandbox keys ONE client/login under both markets). An
unconfigured market returns typed not-ok acks (place/cancel/amend), reads skip it, and
`fetch_venue_orders` **raises** `SettradeMarketNotConfigured` (never `[]` — an empty list would
forge venue truth). The all-sessions heartbeat keeps the single frozen Phase-0 breaker (E28): one
dead app trips it and **mass-cancels both books** (spread legs must not survive one-sided; per-market
breakers would thaw the one-breaker `BrokerAdapter` base). Per-market reconciler GET-budget skip set
(a starved bucket no longer stalls the other client's groups); additive `/health`
`brokers.settrade.sessions = {"SET": bool|None, "TFEX": bool|None}`. **Unchanged:** capability
**cells**, `client.py`/`mapping.py`/`models.py`/`core/`/`db/`/`contracts/`, the PATCH route,
`NormalizedOrder`, `POST /orders` (a spread = two independent submits, one per leg — no batch
endpoint; in-engine refactor, no new `third_party` service). 713 tests passed, mypy strict, 96.22%
coverage. **Real-venue read-only verified** through the refactored adapter against prod broker 023
(equity `9XXXXXXXX` via the `ALGO_EQ` client, TFEX `5XXXXX-X` via the `ALGO` client; both apps'
tokens acquired, PIN never sent). The InnovestX trading PIN is still absent from `.env` — the
explicit `micro_live`-flip prerequisite. Phase plan:
[`phase4.1-settrade-per-market-apps.md`](phase4.1-settrade-per-market-apps.md).

- **Objective:** route SET and TFEX **concurrently** through two OAuth apps behind one adapter,
  with zero contract change — spread-trade ready.
- **Scope (shipped):** per-market client mapping + per-market credentials resolution
  (per-market | shared | mixed; partial-fails-loud); `SettradeMarketNotConfigured`; all-sessions
  heartbeat on the single breaker; per-market reconciler budget skip; additive `sessions` health
  field; per-market boot logging (never secrets).
- **Non-goals (deferred):** a batch / multi-leg / spread endpoint; a `third_party` Settrade
  service; per-market circuit breakers; any capability-cell change.
- **Acceptance (met):** the same `NormalizedOrder` routes SET via the equity app and TFEX via the
  derivatives app concurrently; the sandbox single-app config is unchanged (one login under both
  markets); one dead app trips the breaker and mass-cancels both books; gate green; real-venue
  read-only verified.
- **Quality gate:** ruff + mypy strict + pytest ≥90% on `adapters/settrade*` (respx-mocked; no live
  creds in CI).
- **Cross-refs:** [`phase4-settrade-adapter.md`](phase4-settrade-adapter.md); decision E28 in
  [`.claude/knowledge/decision-log.md`](../../.claude/knowledge/decision-log.md).

### Phase 5 — Strategy execution path + order-update streaming 📡
**Status:** `[x]` **Engine side complete (2026-06-12).** **Repo:** this repo
(`feature/phase5-strategy-execution-path-order-streaming`); infra-db
[PR #15](https://github.com/lumduan/quant-infra-db/pull/15) (`strategy_id` column, open);
gateway streaming-proxy in its own PR after the engine PR. **Shipped:** the normalized
**order-update stream out** (umbrella **D12** realised) + the **dual-provider order book
service** + `SimAdapter` live fill pricing + strategy identity — all **additive and
default-off**, with `live`/`micro_live` gating, the kill-switch path, PTRM, and the frozen
`NormalizedOrder` / 13-edge state machine / capability **cells** unchanged.
`events/` — frozen `OrderUpdateEvent`/`FillEvent`/`GapMarker` (`Decimal`-as-string wire;
`status` via the one existing `to_public_status` mapping, E8); an in-process `EventHub`
(monotonic `seq`, ring-buffer `Last-Event-ID` replay, bounded per-subscriber queues with
drop-oldest + `gap` markers, cid→strategy LRU, **exception-proof `publish`** — the order path
never fails on stream plumbing). **Publish hooks** in the five repository writers
`insert_order` (the `PENDING_NEW` birth) / `ack_order` / `replace_order` / `update_status` /
`apply_fill` — the choke points every one of the 13 frozen edges funnels through (incl. the
kill-switch mass-cancel sweep); `apply_fill` publishes only for newly-inserted fills.
**`GET /orders/stream`** (`api/streams.py`) — SSE, `id:`=seq / `event:`=engine-state frames
(strict 9-state subset + `gap`/`resync_required` advisories), conjunctive
`strategy_id`/`client_order_id` filters, `Last-Event-ID` replay. **D16 strategy identity** —
`X-Strategy-Id` header → the new nullable `execution.orders.strategy_id` (infra-db PR #15);
events echo it; the stream **DB-seeds** a strategy's historical cids at subscribe time so
reconciler-discovered events for pre-restart orders match. `order_book/` — normalized frozen
`OrderBook`/`OrderBookLevel`, an in-memory LRU cache with refcounted SSE fan-out, a **Settrade**
provider (SDK realtime behind a lazy `_import_sdk` seam, all blocking SDK work on
`asyncio.to_thread`, `call_soon_threadsafe` bridge — **E21 order-routing SDK ban unchanged**), a
**Liberator** provider (ws-ticket + raw `websockets` Engine.IO v4 client, **no `curl_cffi`**,
default-namespaces-then-`BidOfferV2` join order, mid-session live join/leave, jittered reconnect
with fresh ticket + re-join), a failover **router** (N consecutive errors in a window →
secondary + structured `order_book.provider_switch`; per-symbol overrides; no auto-failback),
and a lifespan-wired **runtime** (default off — bit-for-bit unchanged engine). **`GET
/order-book/{symbol}`** (404 cold; market omitted probes SET→TFEX) + **`/stream`**; additive
`/health` `order_book` block. **`SimAdapter` live pricing** (D21) — `SimFillPricer`: book best
ask/bid (limit-bounded) → market-data engine last 1d close (limit-bounded) → `None` (the
adapter's own `_reference_price`); price-only — with **no source injected the adapter is
bit-for-bit Phase-2**. Decisions **D14–D24** (the cross-cutting D-series continuation); open
questions **§H–§K**. New deps `websockets` + lazy market-data-only `settrade-v2`. **853 tests,
95.72% cov**, mypy strict, ruff clean. **D23 deferred to §I** (a Settrade native-push
reconcile-kick would breach the D18 SDK containment — the SDK may not leak into `adapters/`).
The strategy-side acceptance (a strategy's signal→order→fill→position sim loop) **moves to
Phase 5.1**. Phase plan:
[`phase5-strategy-execution-path-order-streaming.md`](phase5-strategy-execution-path-order-streaming.md).
**Phase 5.1 unblocked.**

- **Objective (engine side):** ship the normalized order-update stream out + the order book
  service so a strategy can react to fills/rejects/transitions without polling.
- **Scope (shipped):** the normalized **order-update stream out** (SSE; gateway proxies it);
  in-engine dual-provider order book (Settrade realtime + Liberator WS) → `SimAdapter` live
  fill prices + snapshot/SSE reads; `X-Strategy-Id` identity persisted (infra-db PR #15);
  D14–D24; §H–§K.
- **Non-goals:** the strategy-repo flags + sim trade loop (→ **Phase 5.1**); any change to
  `live`/`micro_live` gating, the kill-switch, PTRM, or the frozen contracts; per-strategy
  auth (§J); Settrade native push as a direct transition source (§I/D23); multi-worker fan-out
  (§H); durable book storage / depth > 10 / auto-failback (§K).
- **Acceptance (met):** sim order → `PENDING_NEW → NEW → FILLED` on the stream (partials one
  event each); kill-switch sweep events stream; `Last-Event-ID` reconnect misses nothing in the
  ring, else `resync_required`; `?strategy_id=` filters (incl. DB-seeded pre-restart orders);
  both order-book parsers produce identical normalized books; failover switches + logs;
  `SimAdapter` fills at best bid/offer when warm and falls back (and logs) when cold; with no
  source injected, Phase-2 bit-exact; gate green; `live` gating unchanged.
- **Quality gate:** ruff + mypy strict + pytest ≥90% on `adapters/` + state machine +
  `order_book/` + `events/` (853 tests, 95.72% cov).
- **Cross-refs:** umbrella ROADMAP Phase 5; D14–D24 in
  [`.claude/knowledge/decision-log.md`](../../.claude/knowledge/decision-log.md); the stream +
  order-book references in
  [`.claude/knowledge/order-update-stream.md`](../../.claude/knowledge/order-update-stream.md),
  [`.claude/knowledge/order-book-service.md`](../../.claude/knowledge/order-book-service.md).

### Phase 5.1 — Strategy execution flags + sim trade loop 📈
**Status:** `[x]` **Complete (2026-06-12).** **Repos:** `strategies/csm-set` (PR #16 →
`live-test`), `strategies/tfex-s50-multi-tf-swing` (PR #18 → `main`), plus a one-line
`quant-api-gateway` fix (PR #24 — forward `X-Strategy-Id` in `_proxy`/`_proxy_sse`; the proxy
had been stripping it, so D16 attribution could not work through the gateway). The split-out
strategy-side scope: strategies became first-class callers of the Phase 5 engine surface.

- **Objective (met):** a strategy runs an end-to-end **sim** trade loop with no broker code in it.
- **Scope (shipped):** csm-set `CSM_EXECUTION_MODE = off|sim|live` (default `off`) + tfex
  `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE`, each with `*_EXECUTION_ACCOUNT`/`*_EXECUTION_BROKER`
  and settings-level gating (`live`+public-mode rejected at construction); per-repo local wire
  mirrors (engine-true enums, Decimal-as-string, no cross-repo imports); `ExecutionEngineAdapter`
  (gateway-only, `X-API-Key` + `X-Strategy-Id`, same-cid transport retry per ADR §A, typed
  envelopes terminal, hand-rolled SSE with `Last-Event-ID` reconnect + client seq watermark +
  connect handshake); `run_sim_loop` (subscribe-before-submit, single-source fill accounting,
  VWAP, `GET /orders/{cid}` residual reconcile; tfex: `position_effect` OPEN/CLOSE inferred
  against the evolving position, flip → typed error); committed verify scripts; **library +
  verify-script only** (pipeline wiring deferred). **No engine code change.**
- **Acceptance (met, live 2026-06-12):** both verify scripts passed through the gateway against
  the owner-mode sim engine — csm `PTT` BUY 100 @ 35.50 FILLED via SSE → position updated;
  tfex `S50Z2026` entry OPEN → exit CLOSE → flat; all three `execution.orders` rows stamped
  with the correct `strategy_id` (gateway forwarding proven); engine restored to the
  broker-free public default. `live` stays gated everywhere.
- **Quality gate:** csm 72 new tests (new modules 96–100% cov; + a CI repair commit for
  pre-existing breakage: `types-PyYAML` dev stubs, docker-smoke external-network creation);
  tfex 81 new tests, suite 747 passed, 97.83% cov; gateway 363 tests, 90.93% cov.
- **Cross-refs:** plan
  [`phase5.1-strategy-execution-flags-sim-trade-loop.md`](phase5.1-strategy-execution-flags-sim-trade-loop.md);
  the strategy consumer contract appended to
  [`.claude/knowledge/order-update-stream.md`](../../.claude/knowledge/order-update-stream.md);
  umbrella ROADMAP Phase 5.

### Phase 6 — Safety, ops & reconciliation hardening 🛡️
**Status:** `[x]` **Complete (2026-06-13).** **Repo:** this repo
(`feature/phase6-safety-ops-reconciliation-hardening`, own PR). **Shipped:** the failure paths
made provably safe under fault injection across **five additive workstreams** — **(A) risk-gate
hardening:** per-account notional/qty caps (`EXECUTION_ENGINE_ACCOUNT_MAX_{NOTIONAL,QTY}` JSON
maps, absent-account → global cap, enforced in EVERY stage incl. `sim`), an advisory price-band
check (`core/price_band.py` reusing a factored-out shared `adapters/market_data.py`
`MarketDataClient`, wired after the PTRM gate, MARKET bypass, WARN+pass on fetch failure, typed
`PriceBandExceeded` 422, default-off), and the **unified duplicate-burst guard** (one guard,
richer `account|symbol|side|qty|order_type|price` fingerprint, `exe:burst:` key, typed
`DuplicateBurstDetected` 409, **default-ON** — superseding the old coarse 2 s/429 guard, the legacy
window setting now unused); **(B) kill-switch admin-trip hardening:** idempotent engage/disengage
(`already_engaged`/409 `kill_switch_not_engaged`), structured JSON `kill_switch.engaged|disengaged`
logs, optional `X-Operator-Id`, `cancelled_count`, + a 5-order (NEW + PARTIALLY_FILLED)
fault-injection test asserting genuine CANCELLED-transition audit rows; **(C) idempotency soak +
reconciliation drift:** PENDING_NEW-stuck / ack-lost / fill-before-ack / same-cid-retry scenarios
(no double-send under a mid-submit kill), DB-behind→FILLED repair, DB-ahead never regresses a
terminal, stranded `PENDING_REPLACE` `replace_resolve`; **(D) per-adapter rate limits:** a
pure-asyncio `adapters/rate_limit.py` `TokenBucket` (monotonic lazy-refill, await-on-deficit,
never drop/raise) — Settrade GET + WRITE buckets per `SettradeClient` (per OAuth app/market) +
a Liberator POST bucket on `place()` only, plus an `EventHub` §H slow-subscriber stress test
(1000 ev/s × 10 slow subscribers — **single-process confirmed, no fan-out code change**); **(E)
structured audit:** owner-mode `GET /admin/orders/{cid}/audit` + streaming NDJSON
`GET /admin/audit/export` (date/`strategy_id` filters, server-side cursor), the response
**synthesized** from the existing `order_events` columns — **no `quant-infra-db` schema change**.
**`live` stays gated; the frozen `NormalizedOrder` / 13-edge state machine / capability cells,
kill-switch-first ordering, and PTRM semantics are all unchanged.** 952 tests (853 baseline →
+99), 96.01% cov, mypy strict, ruff clean. Phase plan:
[`phase6-safety-ops-reconciliation-hardening.md`](phase6-safety-ops-reconciliation-hardening.md).
**Phase 7 unblocked.**

- **Objective:** make the failure paths provably safe under fault injection.
- **Scope (shipped):** pre-trade risk-gate hardening (per-account notional/qty caps, price-band
  vs live market data, duplicate-burst guard); the kill-switch runbook + admin trip; idempotency
  soak + reconciliation drift tests (kill the process mid-submit; assert **no double-send** and
  correct repair); per-adapter rate-limit / backpressure (Settrade is rate-limited natively);
  structured audit export from `order_events`.
- **Non-goals:** no new broker; no new order type.
- **Acceptance (met):** the documented failure-injection suite passes; no double-submit under
  fault; reconciliation provably repairs drift; the kill-switch is verified end-to-end (5-order
  mass-cancel + audit rows + disengage roundtrip).
- **Quality gate:** ruff + mypy strict + pytest ≥90% incl. fault-injection tests (952 tests,
  96.01% cov).
- **Cross-refs:** umbrella ROADMAP Phase 6; tfex risk kill-switch / ladder playbook as a
  safety-design precedent; Phase 6 decisions E29–E35 + the §H revisit in
  [`.claude/knowledge/decision-log.md`](../../.claude/knowledge/decision-log.md).

### Phase 7 — Documentation (tvkit-ref style, AI-agent-first) 📚
**Status:** `[x]` **Complete (2026-06-13).** **Repo:** this repo (PR `docs/phase7-documentation`).
Shipped: the `docs/` hub — `README.md` + `overview.md`, architecture ×4, api ×9 (each with a real
curl example), operations ×4, data ×2 — plus the `.claude/knowledge/` `order-flow` + `deployment`
additions, a new `development-workflow` playbook + the `order-routing-safety` Phase-6 refresh, and the
umbrella `execution-engine-runbook`. Per the verified-code rule, several brief-vs-code divergences
were corrected (the 13 state edges; env `PG_DSN` not `DB_DSN`; `RISK_MAX_ORDERS_PER_SECOND`; no
`API_HOST`/`API_PORT`; `GET /admin/kill-switch`; amend body `new_qty`/`new_client_order_id`; migration
`13_execution_strategy_id.sql`) — itemized in [`phase7-documentation.md`](phase7-documentation.md).

- **Objective:** every endpoint + env var + state transition documented with a real example.
- **Scope (ships):** `docs/` hub + `architecture/` (topology, two-plane boundary, state
  machine), `api/` (order / cancel / amend / status / capabilities / order-update WS — each
  with a real request/response example), `operations/` (bring-up / config / kill-switch /
  troubleshooting), `data/` (order state machine + `execution` schema); `.claude/knowledge/`
  order-flow + deployment + contract docs; an umbrella execution runbook in `.claude/playbooks/`.
- **Acceptance:** every endpoint has a real example; every env var documents default/allowed/
  effect; the order state machine + capability matrix are documented; **no secrets**.
- **Quality gate:** docs-only; links resolve; public-safe scan clean.
- **Cross-refs:** umbrella ROADMAP Phase 7; marketdata-engine `docs/` hub as the format
  precedent.

---

### Phase 8 — `StreamingProAdapter` (the third real broker) 🧩
**Status:** ✅ **Complete (2026-06-16).** Plan:
[`phase8-streaming-pro-adapter.md`](phase8-streaming-pro-adapter.md). Realises
`feature-streaming-pro-adapter` Phase 4 — the engine side of the standalone `settrade-streaming-api`
retail bridge (which shipped its own Phases 0–3).
- **Objective:** add the third real broker (`streaming_pro`) by writing ONE adapter, zero
  frozen-contract change.
- **Scope:** an `adapters/streaming_pro/` subpackage that composes the bridge over **plain httpx**
  (mirrors `LiberatorAdapter` — the HTTP-bridge precedent): transport (`X-API-Key`, redacting),
  mapping (uppercase-enum pass-through, Decimal-as-string, **no PIN** — bridge-owned), the 7 frozen
  methods + `/session/status` heartbeat + reconciler v1; `Broker.STREAMING_PRO`; 2 conservative
  `CapabilitySet` rows (`(MARKET,LIMIT)×DAY`, SET+TFEX, amend `cancel_replace`); 6
  `EXECUTION_ENGINE_STREAMING_PRO_*` settings; stage/router/deps/lifespan wiring;
  `docker-compose.streaming.yml` (bundles the bridge + its `streaming-pro-redis`).
- **Non-goals:** native amend (bridge capture-pending → `cancel_replace`); conditional/multi-leg
  (bridge SP-E); the live `micro_live` soak (operator-driven). **`live` stays gated.**
- **Quality gate:** ruff + mypy strict + pytest ≥90% on `src/` (respx-mocked). ✅ 1034 tests, 95.26%.

---

## Open questions / risks (pinned in Phase 0 / revisit per phase)

> All seven items were **pinned in Phase 0** (2026-06-10) as written decisions in the ADR
> ([`feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md),
> Pinned §A–§G), with the stances + parameters confirmed by the owner. The per-phase revisit
> notes below them still apply.

- **Exactly-once vs at-least-once.** ✅ **PINNED (ADR §A):** the guarantee is
  **at-least-once submission + engine-side dedupe on `client_order_id` + durable state +
  reconciliation + idempotent re-submit — explicitly NOT exactly-once** (neither broker
  echoes a client id, R1); reconciliation never blindly re-sends. `client_order_id`
  generation standard: **UUIDv4** (client-generated, format-validated, opaque to the engine —
  time-ordered schemes like ULID/UUIDv7/Snowflake are acceptable drop-ins since the id is
  never parsed for time).
- **`client_order_id` ↔ `broker_order_id` mapping.** ✅ **PINNED (ADR §B):** the engine
  persists the mapping **atomically with the `PENDING_NEW → NEW` transition**; when an ack is
  lost before the id is recorded, the reconciliation loop fuzzy-matches on
  `(account, symbol, side, qty)` within **±5 s** of the persisted submit timestamp. A stuck
  `PENDING_NEW` resolves via reconciliation within a bounded window — it never blocks
  routing indefinitely.
- **Order-type semantics drift.** ✅ **PINNED (ADR §G):** the `order_type` enum is frozen in
  Phase 0; each `(broker, market, order_type)` combination is treated as a **distinct
  pre-flight validation class** ("Stop"/"ATO"/"ATC"/"iceberg" min-display/session quirks,
  exact Settrade enum spellings) — deliberately **Phase 3/4 adapter work**. The
  `(confirm P4)` cells were **pinned in Phase 4** (2026-06-11) from the official venue docs
  (R4 resolved) — no `(confirm P4)` placeholder remains.
- **Auth liveness.** ✅ **PINNED (ADR §G):** session liveness is adapter-local (D10) — and
  waiting for a 401 on a live order is unacceptable. A **proactive heartbeat worker** polls a
  low-impact read (e.g. account balance) every **~30 s** per adapter; consecutive failures
  trip a **circuit breaker** that halts new routing for that broker and raises an alert.
  Design pinned here; scaffolding wired in Phase 2, exercised per real adapter in Phases 3/4.
- **Amend asymmetry.** ✅ **PINNED (ADR §D/§F):** `BrokerAdapter.amend` is uniform; Settrade
  amends natively; `LiberatorAdapter.amend` is cancel-then-replace — **two venue operations
  under the hood**, therefore **not atomic**: queue-priority loss and a brief
  no-resting-order window are declared in its capability metadata and surfaced to callers,
  never abstracted away.
- **Real-money blast radius.** ✅ **PINNED (ADR §G):** pre-trade risk (PTRM) caps —
  max order value/qty, per-second order rate limit — plus the **global kill-switch (reject
  all new submits AND mass-cancel open orders)** and the sim-default stage (E2/E3) are
  **Phase-2 milestones** (D11), not a Phase-6 afterthought — no live adapter lands before
  they exist.
- **Streaming creep.** ✅ **PINNED (ADR §G):** D1 reaffirmed — execution plane only;
  market-data / order-book streams are strictly **external, read-only dependencies** (e.g.
  price-band checks); never owned here.

### Phase 5 additions (2026-06-12): §H–§K

Continuing §A–§G — pinned-as-deferred in the Phase 5 plan (the §A–§G preamble above is
unchanged):

- **§H — Single-process fan-out.** The `EventHub` is in-process; the engine runs one uvicorn
  worker. Multi-worker / multi-instance fan-out (Redis pub/sub, mirroring the kill-switch
  pattern) is **deferred** until a second worker exists — revisit in Phase 6. **Phase 6 revisit
  conclusion (2026-06-13): CONFIRMED single-process, NOT upgraded.** Phase 6 added no multi-worker
  fan-out; the existing drop-oldest + gap-marker overflow policy (exception-proof `publish`) was
  **verified under a 1000 ev/s × 10-slow-subscriber stress test** (fast subscribers get all events,
  slow get `gap` markers, the publisher never blocks, the order path never raises) and shipped as a
  test only, no code change. Multi-worker / Redis pub-sub stays deferred until a concrete
  second-worker story exists. See decision-log E29–E35 (§H revisit).
- **§I — Settrade push as a transition source + `GET /trades`.** Phase 5 ships at most the D23
  reconcile-kick (itself deferred — it would breach D18 SDK containment). Driving frozen edges
  directly from venue push payloads, and per-fill granularity from `GET /trades` (replacing the
  E18/E24 watermark deltas), stay **deferred** until the push protocol is observed at micro_live.
- **§J — Least-privilege strategy auth.** Per-strategy API keys / JWT claims binding a key to a
  `strategy_id` (submit/read/stream scoped to own orders) are **deferred**; the shared-key +
  `X-Strategy-Id` header model ships now, and the durable column makes the upgrade additive.
- **§K — Order-book extensions.** Durable book persistence, market-impact modelling, depth > 10,
  auto-failback to primary, a 4h/derived-feed story — all **deferred** to the execution plane
  until a concrete consumer exists (D1/D17 discipline).

---

## Cross-references

- Cross-cutting roadmap → [`../../../plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md)
- Feature sub-plan → [`liberator-session-self-heal/ROADMAP.md`](liberator-session-self-heal/ROADMAP.md) — auto-login the Liberator broker session when it dies (refactor + enable the bundled `SessionMonitorService`; proposed 2026-06-13)
- Architecture ADR (Phase-0 gate) → [`../../../.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md)
- Broker research (cited) → [`../../.claude/knowledge/broker-research-liberator.md`](../../.claude/knowledge/broker-research-liberator.md)
  (the Settrade research note was removed with broker-023 on 2026-07-18)
- Capability matrix + contract + state machine →
  [`../../.claude/knowledge/capability-matrix.md`](../../.claude/knowledge/capability-matrix.md),
  [`../../.claude/knowledge/normalized-order-contract.md`](../../.claude/knowledge/normalized-order-contract.md),
  [`../../.claude/knowledge/order-state-machine.md`](../../.claude/knowledge/order-state-machine.md)
- Pattern precedent (standalone credential-owner engine + gateway proxy + rollout flag) →
  `quant-marketdata-engine` (`../../../quant-marketdata-engine/docs/plans/ROADMAP.md`)
- Adapter target (Phase 3) → `liberator-trading-api`
  (`app/services/{set_order,tfex_order,orders}_service.py`, `app/models/{set_order,tfex_order,orders}.py`)
- Engine catalog + Docker network contract + host-port allocation + ingestion idempotency →
  umbrella [`CLAUDE.md`](../../../CLAUDE.md)

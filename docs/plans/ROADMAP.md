# quant-execution-engine — ROADMAP

The **Execution engine**: a single standalone service (host `:8400`, container `:8000`,
gateway-proxied under `/api/v2/engines/execution/*`) that is the **only** thing that sends
orders to brokers. Every strategy submits one canonical `NormalizedOrder`; the engine routes
it to a broker adapter (Liberator, Settrade) or the `SimAdapter`. Strategies **never** speak a
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

**Status: Phases 0–4 complete (Phase 4: 2026-06-11) — ADR accepted; order store live; engine
core + SimAdapter + gateway proxy live; `LiberatorAdapter` + `SettradeAdapter` (both real
venues) live; Phases 5–7 Proposed.** The full order path runs end-to-end through the gateway:
submit/dedupe/fills/cancel/**native-amend** over the durable store, PTRM + kill-switch + stage
ladder wired from the first path. Two real brokers now route the **same** `NormalizedOrder` by
`broker`/account (Liberator cancel+replace amend; Settrade native amend via `PATCH /orders/{cid}`).
**`live` stays gated — no real-money default**; `micro_live` is the highest rung the adapters
exercise, operator-driven. Next: **Phase 5** (strategy execution path + the normalized
order-update stream out).

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

## Broker capability matrix (Liberator vs Settrade vs Sim)

Mapped onto one `NormalizedOrder`. Liberator cells are sourced from the existing
`liberator-trading-api` service models (validated against the live adapter since Phase 3);
**Settrade cells are pinned (2026-06-11) from the official venue docs** at
`developer.settrade.com/.../investor-{derivatives,equity}/*.md` (the raw markdown backend of
the JS SPA) cross-checked against the `settrade-v2` 2.2.1 SDK source — every former
**(confirm P4)** cell is now a concrete entry. See
[`.claude/knowledge/capability-matrix.md`](../../.claude/knowledge/capability-matrix.md) for
the cited extraction and the Phase 4 validation section.

| Capability | **Liberator** (SET / TFEX) | **Settrade** (SET + TFEX) | **Sim** |
|---|---|---|---|
| Auth model | OTP/2FA + SMS-webhook refresh; Redis-backed token; per-order **PIN** | OAuth app creds (`app_id`/`app_secret`/`app_code`/`broker_id`) → access+refresh token (ECDSA P-256 login sig, single-flight refresh), rate-limited; per-order **PIN** | none |
| Markets | **SET** (equity) + **TFEX** (derivatives) | **SET** (equity, `/api/seos/v3`) + **TFEX** (derivatives, `/api/seosd/v3`) | any (configurable) |
| Place order | `POST /order/place/{set,tfex}` | `POST /{broker_id}/accounts/{account_no}/orders` (raw httpx, not the SDK) | in-proc |
| `side` | SET `Buy/Sell`; TFEX `Long/Short` | SET `Buy/Sell`; TFEX `Long/Short` | both |
| `position_effect` | TFEX `Open/Close/Auto`; SET n/a | TFEX `Open/Close` (`Auto` undeclared — extra permission); SET n/a | both |
| MARKET / LIMIT | ✅ (`Market`/`Limit`) | ✅ (`MP-MKT` / `Limit`) | ✅ |
| STOP / STOP_LIMIT | TFEX ✅ (`priceType=Stop` + `stopSymbol`/`stopPrice`/`stopCondition`); SET ✗ | TFEX ✅ (`MP-MKT`/`Limit` + stop trio); **SET ✗** (no equity stop API) | ✅ |
| ICEBERG (display qty) | ✅ `icebergVol` (SET + TFEX) | ✅ SET `qtyOpen` / TFEX `icebergVol` (`Limit` base) | ✅ |
| ATO / ATC | SET ✅ (`priceType=ATO/ATC`); TFEX ✗ | SET ✅ (`ATO`/`ATC`); TFEX `ATO` ✅, **`ATC` ✗** (not a derivatives priceType) | ✅ |
| MTL / MP (market-to-limit) | SET ✅ (`priceType=MP`) | SET + TFEX ✅ (`MP-MTL`) | ✅ |
| TIF | `Day/GTC/IOC/FOK` | `Day`/`IOC`/`FOK`/`GTC('Cancel')`; **`Date`(GTD) undeclared** (no `Tif` member) | all |
| Amend | ✗ **no amend route** → adapter does **cancel + replace** | ✅ **native** `PATCH /orders/{order_no}/change` (`newPrice?`/`newVolume?`/SET `newIcebergVolume?`) over `PENDING_REPLACE → NEW` | ✅ native |
| Cancel | `POST /order/cancelled/{set,tfex}` by `orderNo` list (≤50) + PIN | `PATCH /orders/{order_no}/cancel` + bulk `PATCH /cancel` + PIN | ✅ |
| Query (reconcile) | `GET /orders`, `/orders/{account_no}`, `/orders/summary` (status, matched/balance/cancelled, reject_code, can_cancel) | `GET /orders` (cumulative `matchQty`/`matched`, `rejectCode`/`rejectReason`, `canCancel`/`canChange`); `GET /trades` reserved for Phase 5 | in-proc state |
| Order-update stream | indirect: `POST /ws-ticket` issues a venue WS ticket (no normalized push) | **native** `RealtimeDataConnection.subscribe_{derivatives,equity}_order(...)` (MQTT) — **Phase 5** | synthetic events |
| Client idempotency key | ✗ (broker `orderNo` only) | ✗ (broker `orderNo`/`order_no` only) | n/a |

**Two findings that shape the design:**
- **Neither broker accepts a client idempotency key.** The engine therefore owns the
  `client_order_id` ↔ `broker_order_id` mapping in its durable store, and dedupe happens in
  the engine **before** routing (D5). This is the crux of exactly-once-ish submission.
- **Amend is asymmetric.** Settrade amends natively; Liberator has no amend endpoint. The
  `BrokerAdapter.amend()` contract is uniform, but `LiberatorAdapter.amend` is implemented as
  an atomic **cancel-then-replace** (declared in its capability set so the router/strategy
  knows the semantics differ).

---

## `NormalizedOrder` / `NormalizedOrderResult` contract (frozen in Phase 0; realised in Phase 2)

```text
NormalizedOrder
  client_order_id : str          # client-generated idempotency key (UUID/ULID)
  broker          : "sim" | "liberator" | "settrade"
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
`BUY`→ Liberator SET `Buy` / TFEX `Long`, Settrade `Long`; `SELL`→ `Sell` / `Short`.
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
| **`quant-execution-engine`** (this repo) | The engine: `BrokerAdapter` interface, `NormalizedOrder` contract + capability matrix, order state machine + idempotency, reconciliation loop, pre-trade risk gate + kill-switch, order-update stream, `SimAdapter`, `LiberatorAdapter`, `SettradeAdapter`. |
| **`quant-infra-db`** | New `execution` schema (Phase 1): `orders` / `fills` / append-only `order_events`. Its own PR. |
| **`quant-api-gateway`** | Proxy route `/api/v2/engines/execution/*` → `:8400` + register the 5th engine (Phase 2). Its own PR. **No broker credential.** |
| **`liberator-trading-api`** (existing) | Becomes a `LiberatorAdapter` **target** over HTTP (Phase 3), **vendored as a git submodule** under `third_party/liberator-trading-api/` and bundled into this repo's owner-mode bring-up (`docker-compose.liberator.yml`) as an **internal-only** upstream (no host port). May add a normalized order-update hook. No strategy calls it directly anymore. |
| **`strategies/csm-set`, `strategies/tfex-s50-multi-tf-swing`** | Gain an execution path behind `*_EXECUTION_MODE = off\|sim\|live` (Phase 5). Submit `NormalizedOrder`s; never speak a broker API. |

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

### Phase 5 — Strategy execution path + order-update streaming 📡
**Status:** `[ ]` Proposed (**unblocked 2026-06-11** by Phase 4). **Repos:** `strategies/csm-set`,
`strategies/tfex-s50-multi-tf-swing`, this repo (own PRs). Settrade's native MQTT push
(`subscribe_{derivatives,equity}_order`) feeds the normalized stream directly; Liberator's is
poll-reconciled into the same shape (R3). The Settrade adapter's `GET /trades` surface is
reserved for this phase (the reconciler v1 deliberately uses cumulative-watermark deltas, not
per-fill trades).

- **Objective:** a strategy runs an end-to-end **sim** trade loop with no broker code in it.
- **Scope (ships):** the normalized **order-update stream out** (WS/events) of fills + status
  changes (D12; gateway proxies the WS) — Settrade's native `subscribe_derivatives_order`
  feeds it, Liberator's is polled/reconciled into the same shape; csm-set gains
  `CSM_EXECUTION_MODE = off|sim|live` (default `off`/`sim`); tfex gains
  `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE`, exercising TFEX `position_effect` (OPEN/CLOSE) +
  futures order types.
- **Non-goals:** no strategy sends `live` orders (cutover stays behind the flag, default sim).
- **Acceptance:** a strategy runs signal → `NormalizedOrder` → fill stream → position entirely
  in sim, with no broker code in the strategy; `live` remains explicitly flagged + gated.
- **Quality gate:** each repo's own gate; engine ≥90% on the stream + adapter glue.
- **Cross-refs:** umbrella ROADMAP Phase 5; tfex execution-mode flag mirrors its OHLCV
  `mirror|engine` reader-flag rollout.

### Phase 6 — Safety, ops & reconciliation hardening 🛡️
**Status:** `[ ]` Proposed. **Repo:** this repo (own PR).

- **Objective:** make the failure paths provably safe under fault injection.
- **Scope (ships):** pre-trade risk-gate hardening (per-account notional/qty caps, price-band
  vs live market data, duplicate-burst guard); the kill-switch runbook + admin trip; idempotency
  soak + reconciliation drift tests (kill the process mid-submit; assert **no double-send** and
  correct repair); per-adapter rate-limit / backpressure (Settrade is rate-limited natively);
  structured audit export from `order_events`.
- **Non-goals:** no new broker; no new order type.
- **Acceptance:** the documented failure-injection suite passes; no double-submit under fault;
  reconciliation provably repairs drift; the kill-switch is verified.
- **Quality gate:** ruff + mypy strict + pytest ≥90% incl. fault-injection tests.
- **Cross-refs:** umbrella ROADMAP Phase 6; tfex risk kill-switch / ladder playbook as a
  safety-design precedent.

### Phase 7 — Documentation (tvkit-ref style, AI-agent-first) 📚
**Status:** `[ ]` Proposed. **Repo:** this repo (own PR).

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

---

## Cross-references

- Cross-cutting roadmap → [`../../../plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md)
- Architecture ADR (Phase-0 gate) → [`../../../.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md)
- Broker research (cited) → [`../../.claude/knowledge/broker-research-liberator.md`](../../.claude/knowledge/broker-research-liberator.md),
  [`../../.claude/knowledge/broker-research-settrade.md`](../../.claude/knowledge/broker-research-settrade.md)
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

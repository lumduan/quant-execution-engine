# Phase 2: Engine Core + Gateway Proxy + SimAdapter

**Feature:** feature-execution-engine — Phase 2: engine core + gateway proxy + `SimAdapter`
**Branch:** `feat/phase2-engine-core` (this repo) / `feat/v2-execution-proxy` (quant-api-gateway) / `feat/execution-registry-reject-reason` (quant-infra-db)
**Created:** 2026-06-10
**Status:** In Progress
**Depends On:** Phase 0 (Complete — ADR ACCEPTED), Phase 1 (Complete — `execution` order store live)

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Architecture & Submit Flow](#architecture--submit-flow)
6. [Implementation Steps](#implementation-steps)
7. [File Changes](#file-changes)
8. [Success Criteria](#success-criteria)
9. [Completion Notes](#completion-notes)

## Overview

### Purpose

Prove the full order lifecycle end-to-end against sim, with safety wired from the first
path: the FastAPI order surface, the frozen `BrokerAdapter` interface, the
`NormalizedOrder` Pydantic contract, the order state machine over the Phase-1 store,
idempotent submit deduped on `client_order_id`, the PTRM pre-trade risk gate + global
kill-switch (reject new + mass-cancel open), circuit-breaker scaffolding, the
deterministic `SimAdapter`, the `EXECUTION_ENGINE_STAGE` ladder (default `sim`), and the
gateway proxy `/api/v2/engines/execution/*` → `:8400`. **No real-money path exists in
Phase 2.**

### Parent Plan Reference

- Per-service ROADMAP: [`ROADMAP.md`](ROADMAP.md) — Phase 2 section (scope/acceptance/gate
  realised verbatim).
- Cross-cutting: umbrella `plans/feature-execution-engine/ROADMAP.md` Phase 2.
- The ADR (ACCEPTED 2026-06-10): umbrella `.claude/knowledge/feature-execution-engine.md`
  (§A–§G pins this phase implements).
- Phase 1 store: [`phase1-execution-order-store.md`](phase1-execution-order-store.md) —
  the DB triggers this engine rides on.
- Service-shape precedent: `quant-marketdata-engine` (app factory + lifespan, asyncpg
  pool singleton, owner-mode deps, own Redis sidecar, TestClient + dependency_overrides).
- Gateway-proxy precedent: `quant-api-gateway/src/api/v2/engines/market_data.py`.

### Key Deliverables

- This repo: `contracts/`, `core/`, `adapters/`, `db/`, `cache/`, `api/` packages —
  the full sim order path with ≥90% coverage; admin kill-switch endpoints; docs.
- `quant-api-gateway`: `/api/v2/engines/execution/*` thin proxy (POST/GET/DELETE), engine
  catalog entry, no credential.
- `quant-infra-db` (tiny): `engine_registry` row for `execution` (the catalog is
  DB-first) + `execution.orders.reject_reason` column (the Phase-1-sanctioned ALTER).
- Umbrella: pins + status flips after all sub-repo PRs merge.

## AI Prompt

The following prompts initiated this phase:

```
start phase 2 by create implement plan first
```

followed by (when asked how much of Phase 2 this session should execute):

```
i want you recommend and best practice
```

Resolution: full Phase 2, plan-doc-first (this document is the first commit of the
engine PR), then engine → gateway → infra-db PRs merged in sequence with green gates,
then umbrella pins — the Phase 1 pattern.

## Scope

### In Scope

| Item | Repo |
|---|---|
| `NormalizedOrder`/`NormalizedOrderResult` Pydantic contracts + frozen enums + typed error taxonomy | this repo |
| `core/state_machine.py` — pure frozen 13-edge graph (app-side guard; DB trigger is the backstop) | this repo |
| `OrderRouter.submit/cancel/get` — kill-switch-first pipeline, dedupe, capability gate, PTRM, stage gate, single-flight | this repo |
| `BrokerAdapter` ABC (frozen 7 methods) + circuit-breaker scaffolding + deterministic `SimAdapter` | this repo |
| asyncpg repositories over the Phase-1 store (never writing `order_events`) | this repo |
| Redis: single-flight submit lock, rate/burst counters, runtime kill-switch trip | this repo |
| API: `POST /orders`, `GET /orders/{id}`, `DELETE /orders/{id}`, `GET /capabilities`, `GET /health`, `/admin/kill-switch*` | this repo |
| `EXECUTION_ENGINE_STAGE` ladder (default `sim`) + public-mode gating + optional API key | this repo |
| Gateway proxy `/api/v2/engines/execution/*` + catalog entry | quant-api-gateway |
| `engine_registry` `execution` row + `orders.reject_reason` column | quant-infra-db |
| Test suites ≥90% both repos; live end-to-end acceptance run | all |

### Out of Scope (deferred, per ROADMAP)

- Real broker adapters, sessions, OTP/PIN, reconciliation against a venue (Phases 3–4).
- Amend HTTP route (adapter `amend` is implemented + tested; first native amend route
  lands Phase 4 — see Design Decision 7).
- Heartbeat poll worker (~30 s) — scaffolding only; lands with real adapters.
- Price-band risk checks against live market data (Phase 6 hardening).
- Order-update stream endpoint (Phase 4, with the first native push).
- `metadata` persistence (opaque strategy tags; consumed in-flight only).

## Design Decisions

1. **Synchronous in-request sim fills.** The acceptance must observe a complete
   deterministic lifecycle from one POST; background tasks would add ordering
   nondeterminism for zero benefit on an in-proc adapter. The seam is preserved:
   `repositories.apply_fill()` is standalone — Phase 3/4 stream/reconcile workers call
   it identically.
2. **Frozen 6-value `status` + additive `engine_state`.** The public Result enum stays
   untouched; `engine_state` carries the internal 9-state truth (mapping:
   `PENDING_NEW→NEW`; `PENDING_CANCEL`/`PENDING_REPLACE→PARTIALLY_FILLED` if
   `filled_qty>0` else `NEW`). Overloading the frozen enum would break the contract;
   hiding the pending states would hide exactly the reconciliation window ADR §B cares
   about. Recorded as a contract addendum in `.claude/knowledge/normalized-order-contract.md`.
3. **MARKET-family notional basis = `coalesce(price, stop_price)`; absent ⇒ skip with
   WARNING (quantity cap still binds).** Any synthetic basis is fiction; live market
   data is out of scope (Phase 6); rejecting all unpriced orders would forbid MARKET in
   sim. No real-money path exists in Phase 2, so the residual gap is sim-only.
4. **Single-flight lock-miss ⇒ poll the store briefly, then 409 `submit_in_flight`.**
   The Redis lock is contention politeness; the orders PK is the correctness backstop
   (Redis down ⇒ lock trivially acquired ⇒ INSERT collides ⇒ prior result returned).
5. **Admin kill-switch endpoints ship now** (engine-direct, owner-mode + API key, never
   gateway-proxied). Mass-cancel is only meaningful with a runtime trip — an env-only
   switch requires a restart, during which nothing mass-cancels. The env flag remains
   the boot-time backstop and wins over a runtime disengage (409).
6. **`GET /capabilities` returns the full static matrix for all three brokers** with
   `adapter_installed: false` on liberator/settrade and every Settrade `(confirm P4)`
   cell omitted. The capability gate must reject real-broker-impossible orders even in
   sim stage (that is the acceptance's "rejects unsupported"); declaring unconfirmed
   venue enums would violate D7's declare-don't-pretend.
7. **No amend HTTP route.** The ROADMAP Phase 2 surface is frozen-minimal
   (POST/GET/cancel/capabilities); ADR §D freezes `amend` as an adapter method.
   `SimAdapter.amend` is implemented + unit-tested (incl. the `PENDING_REPLACE → NEW`
   repository walk); the route lands Phase 4 with the first native amend.
8. **Contracts live in `contracts/`, the lowest layer.** One model is simultaneously
   the HTTP body, the router's domain object, and the adapter input — duplicating it in
   `api/schemas.py` recreates the drift hazard Phase 1 avoided. `api/schemas.py` holds
   transport envelopes only.
9. **`reject_reason` is a durable column** (added in the quant-infra-db PR, sanctioned
   by the Phase 1 plan), not a cache entry: real-money audit must not live in a 7-day
   Redis key. The engine persists it on REJECTED rows and returns it in results.
10. **Kill-switch precedes even dedupe.** Hard rule #3 ("checked first in the submit
    path") wins over the frozen validation list's step ordering: an engaged switch
    returns 503 for every submit, including resends of known ids. Cancels are NOT
    blocked by the kill-switch — they reduce risk, and mass-cancel itself uses the
    cancel path.
11. **PTRM Redis failure policy is stage-aware**: rate/burst checks fail-open with a
    WARNING in `sim|paper`, fail-closed (`risk_rejected`, cap `risk_backend_down`) in
    `micro_live|live` — coded now even though only sim/paper are reachable in Phase 2.

## Architecture & Submit Flow

```
src/quant_execution_engine/
├── errors.py                # ExecutionEngineError root
├── logging_config.py
├── config/settings.py       # pydantic-settings, env_prefix EXECUTION_ENGINE_
├── contracts/               # lowest layer: enums, orders, capabilities, wire errors
├── core/                    # state_machine, risk, kill_switch, stage, router
├── adapters/                # base (7-method ABC), session (breaker), sim
├── db/                      # postgres pool singleton, models, repositories
├── cache/                   # redis singleton, single_flight, counters
└── api/                     # main (factory+lifespan), routes, deps, schemas, error_handlers
```

Submit pipeline (`POST /orders` → `OrderRouter.submit`):

```
public-mode 403 → kill-switch 503 (FIRST) → Pydantic parse (UUIDv4 + cross-field)
→ dedupe by PK (hit ⇒ prior result, 200) → capability gate (422)
→ PTRM: qty cap / notional / rate / duplicate-burst (422 / 429)
→ stage gate ⇒ SimAdapter (sim|paper) or 403 stage_rejected (micro_live|live)
→ breaker.guard() → single-flight lock (miss ⇒ poll store ⇒ 200 dup | 409)
→ INSERT PENDING_NEW (durable before venue I/O)
→ adapter.place — reject ⇒ REJECTED + reject_reason
                — ack ⇒ ONE UPDATE status=NEW + broker_order_id  (§B atomic; trigger audits)
→ fills applied synchronously (PARTIALLY_FILLED → FILLED)
→ IOC remainder ⇒ PENDING_CANCEL → CANCELLED
→ 201 + NormalizedOrderResult (Decimal-as-string)
```

SimAdapter determinism: pure function of the order. Reference price LIMIT/STOP_LIMIT→
`price`, STOP→`stop_price`, MARKET/MTL/ATO/ATC→`coalesce(price, stop_price,
sim_default_fill_price)`. Fill plan from `metadata`: absent ⇒ one full fill;
`"sim_fills": [q…]` (partial/full/resting); `[]` ⇒ rests NEW; invalid ⇒ REJECTED;
`"sim_reject": "reason"` ⇒ REJECTED. FOK requires a single full fill; IOC cancels the
remainder. Ids: `SIM-{cid[:8]}`, `SIMF-{cid[:8]}-{i}`.

## Implementation Steps

1. [x] quant-infra-db PR: `engine_registry` `execution` row + `orders.reject_reason`
       (live-applied twice, idempotent; infra suites green).
2. [ ] This plan doc (first commit); ROADMAP Phase 2 `[ ]` → `[~]`.
3. [ ] `contracts/` (enums, orders, capabilities, errors).
4. [ ] `db/` (pool, models, repositories) + `cache/` (client, single_flight, counters).
5. [ ] `core/` (state_machine, risk, kill_switch, stage, router).
6. [ ] `adapters/` (base, session, sim).
7. [ ] `api/` (settings, deps, schemas, error_handlers, routes, main rewire).
8. [ ] Test suites; gate green (ruff, mypy strict, pytest ≥90%).
9. [ ] Local `docker compose up -d --build`; engine-direct verification.
10. [ ] Gateway PR: proxy + catalog static entry + config + tests + docs; gate green.
11. [ ] Live end-to-end acceptance through the gateway (public-mode 403 → owner-mode
        lifecycle → dedupe → partial fills → cancel → typed rejects → kill-switch
        mass-cancel → audit rows in `db_execution`).
12. [ ] Docs/status flips here (CHANGELOG, CLAUDE banner, ROADMAP `[x]`, knowledge
        addenda); PRs merged ① → ②; umbrella pins + flips (PR ③).

## File Changes

| File | Action | Description |
|---|---|---|
| `docs/plans/phase2-engine-core-simadapter.md` | Created | This plan |
| `src/quant_execution_engine/{errors,logging_config}.py` | Created | Root error + logging |
| `src/quant_execution_engine/config/*` | Created | Settings (env prefix, PTRM caps, stage, sim knobs) |
| `src/quant_execution_engine/contracts/*` | Created | Frozen contract models/enums/capabilities/wire errors |
| `src/quant_execution_engine/core/*` | Created | State machine, risk, kill switch, stage, router |
| `src/quant_execution_engine/adapters/*` | Created | BrokerAdapter ABC, breaker scaffolding, SimAdapter |
| `src/quant_execution_engine/db/*` | Created | asyncpg pool, row models, repositories |
| `src/quant_execution_engine/cache/*` | Created | Redis client, single-flight, counters |
| `src/quant_execution_engine/api/*` | Rewritten/Created | App factory + lifespan, routes, deps, envelopes, error handlers |
| `tests/*` | Created | Fakes + unit suites + integration marker |
| `.env.example`, `pyproject.toml`, `CHANGELOG.md`, `CLAUDE.md`, `docs/plans/ROADMAP.md`, `.claude/knowledge/*` | Modified | Config knobs, marker, status flips, contract addendum |
| quant-api-gateway: `src/api/v2/engines/execution.py`, `catalog.py`, `router.py`, `src/config.py`, `src/main.py`, tests, docs | Created/Modified | Thin proxy (no credential) |
| quant-infra-db: `07_engine_catalog.sql`, `12_schema_execution.sql`, test, CHANGELOG | Modified | Registry row + reject_reason (merged separately) |

## Success Criteria

- [ ] A `NormalizedOrder` POSTed **through the gateway** routes to `SimAdapter`,
      persists a full lifecycle in `db_execution` (one `order_events` row per
      transition; ack row snapshots `broker_order_id`).
- [ ] Resend of the same `client_order_id` returns the prior result (200, no new rows).
- [ ] Unsupported `(broker, market, order_type, tif)` ⇒ 422 `capability_unsupported`;
      over-cap ⇒ 422/429 `risk_rejected`; wrong stage ⇒ 403 `stage_rejected`;
      kill-switch ⇒ 503 `kill_switch_engaged` + mass-cancel of open orders;
      public mode ⇒ 403 `public_mode` on submission endpoints.
- [ ] Decimals serialize as strings end-to-end; `raw` never crosses the boundary;
      no credential exists in the gateway; **no real-money path exists**.
- [ ] Engine gate green (ruff, mypy strict, pytest ≥90% incl. `adapters/` + state
      machine); gateway gate green; infra-db gate green.
- [ ] ROADMAP/CLAUDE/knowledge statuses updated; PRs sequenced ②b → ① → ② → umbrella ③.

## Completion Notes

_To be filled when the phase completes._

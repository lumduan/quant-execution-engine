# Phase 8: StreamingProAdapter — Third Broker (retail Streaming Pro bridge, HTTP-composed)

> **Status:** Complete (2026-06-16) — 1034 tests, 95.26% cov (3.11+3.12); ruff + mypy strict +
> pip-audit clean. `live` stays gated; no real order (micro_live soak is an operator follow-up).
> **Branch:** `feat/phase4-streaming-pro-adapter`
> **Realises:** `feature-streaming-pro-adapter` **Phase 4** (the engine side) — the bridge
> (`settrade-streaming-api`) shipped its Phases 0–3 standalone; this adds the engine adapter.
> **Builds on:** [`phase3-liberator-adapter.md`](phase3-liberator-adapter.md) (the HTTP-bridge
> precedent). (The Phase-4 Settrade adapter plan was removed with broker-023 on 2026-07-18.)

## Table of Contents

- [Overview](#overview)
- [AI Prompt](#ai-prompt)
- [Scope](#scope)
- [Design Decisions](#design-decisions)
- [Implementation Steps](#implementation-steps)
- [File Changes](#file-changes)
- [Success Criteria](#success-criteria)
- [Completion Notes](#completion-notes)

## Overview

### Purpose

Add the **third** real broker — `streaming_pro` — by writing **one adapter** and changing **no frozen
contract**. The adapter composes the standalone bridge `settrade-streaming-api` (host :8700) over
**plain `httpx`**, structurally identical to how `LiberatorAdapter` composes `liberator-trading-api`.
The bridge is a **dumb transport** (it holds the retail credentials + speaks the broker's JSON REST
order API for SET `fis` + TFEX `seosd`); this adapter puts the **engine's** kill-switch / PTRM /
idempotency / 13-edge state machine / capability gate in front of it — none of which the bridge has.

The engine holds **only the bridge's api-key** (+ base URL): the bridge owns USERNAME/PASSWORD/PIN, so
the adapter stamps **no PIN** — a genuine simplification over Liberator/Settrade. `curl_cffi` stays
**bridge-only**; the engine `src/` is `httpx`-only.

### Parent references

- Umbrella feature ROADMAP: [`../../../plans/feature-streaming-pro-adapter/`](../../../plans/feature-streaming-pro-adapter/)
  (the bridge's `ROADMAP.md` Phase 4 = this; `reference/ORDER_MAP.md` = the live-captured REST API).
- Frozen contracts: [`normalized-order-contract.md`](../../.claude/knowledge/normalized-order-contract.md),
  [`order-state-machine.md`](../../.claude/knowledge/order-state-machine.md),
  [`capability-matrix.md`](../../.claude/knowledge/capability-matrix.md).
- Bridge contract (what the transport calls): `third_party/settrade-streaming-api/` (`/api/v1/*`,
  `X-API-Key`, owner-mode order endpoints).

## AI Prompt

```
start Phase 4 — engine StreamingProAdapter

Add the third real broker (broker key `streaming_pro`) to quant-execution-engine by writing ONE
adapter that composes the settrade-streaming-api bridge over plain httpx — mirror LiberatorAdapter
(the HTTP-bridge precedent) 1:1. No frozen-contract change (BrokerAdapter interface / state machine /
capability shape / order-update stream untouched — additive rows + wiring only).

Key facts: the engine holds ONLY the bridge api-key + base_url (the bridge owns USERNAME/PASSWORD/PIN
-> the adapter stamps NO PIN). The bridge's order surface is JSON REST: POST /api/v1/order/place/{set,
tfex}, POST /api/v1/order/cancel (market-aware batch), GET /api/v1/orders?account=&market=, GET
/api/v1/portfolio, GET /api/v1/account-info, GET /api/v1/capabilities, GET /api/v1/session/status
(heartbeat probe), all X-API-Key. Native amend is capture-pending on the bridge -> amend =
cancel_replace (like Liberator). Conservative capability cells: (MARKET, LIMIT) x (DAY), SET + TFEX,
expand as live-verified. curl_cffi stays bridge-only.

Ship build + respx-unit-tested (>=90% on src/, 3.11+3.12); a live micro_live soak is a later operator
step (like the Liberator/Settrade adapters). live stays gated. 2-level pin (engine PR -> umbrella pin).

Model rules: Thinking/Planning/Design/Fix/Review = Opus MAX; Coding/Testing = Opus X-High; Docs/
Commit/PR = Sonnet. Constraints: uv run only; httpx-only in src/ (curl_cffi banned); secrets SecretStr
+ never logged; account numbers never logged.
```

## Scope

### In scope

- `Broker.STREAMING_PRO` enum member; 2 conservative `CapabilitySet` rows (SET + TFEX); 6
  `EXECUTION_ENGINE_STREAMING_PRO_*` settings (base_url, api_key, heartbeat/breaker/reconcile, rate
  limit — **no PIN**).
- A new `adapters/streaming_pro/` subpackage mirroring `adapters/liberator/`: transport, mapping,
  models, errors, adapter (7 methods + heartbeat + cancel resolver/cache), heartbeat loop, reconciler
  v1, runtime singleton.
- Wiring: `core/stage.py` resolver, `core/router.py`, `api/deps.py`, `api/main.py` lifespan.
- `docker-compose.streaming.yml` (bundle the bridge + its `streaming-pro-redis` sidecar, mirror
  `docker-compose.liberator.yml`).
- Tests ≥90% (respx-mocked) + stage-matrix + capability tests; docs.

### Out of scope

- **Native amend** (the bridge `/order/change` is 501 capture-pending) → `cancel_replace`.
- **Conditional / multi-leg** (bridge SP-E, deferred).
- **TFEX positions read** (the `seosd` portfolio endpoint wasn't captured) → `get_positions` returns SET
  positions; TFEX `[]` + a noted follow-up.
- **Live `micro_live` soak** — operator-driven later (bridge deployed + owner mode + market hours).
- Any bridge change (it's frozen for this phase → 2-level pin, not 3).

## Design Decisions

1. **Mirror `LiberatorAdapter`** — the HTTP-bridge precedent. Plain httpx; `X-API-Key` header;
   redacting transport (account numbers never logged); no PIN (bridge-owned).
2. **Amend = `cancel_replace`** — bridge native amend capture-pending; `amend()` returns
   `AmendAck(ok=False, semantics="cancel_replace", …)`; router orchestrates; cell `amend="cancel_replace"`.
3. **Cancel resolution** — SET cancel needs `ext_order_no`+`symbol` (TFEX only `order_no`). Resolve
   cid→`(order_no, market, account, symbol)` from a warm cache (seeded at place) + the injected store
   resolver; for SET fetch `ext_order_no` via a `GET /orders` lookup when absent.
4. **Conservative capability** — `(MARKET, LIMIT) × (DAY)`, SET + TFEX (LIMIT/DAY live-verified; MARKET
   maps to the bridge's `Market`). Expands to MTL/ATO/ICEBERG + IOC/FOK as each is live-verified.
5. **Heartbeat = `GET /session/status`** (retail-session liveness), not `/health`. Breaker trip →
   `broker_circuit_open` + router mass-cancel; recovers passively after the operator re-logs-in the
   bridge.
6. **Reconciler v1** — mirror Liberator (lost-ack fuzzy match + watermark fills from the row's matched
   qty); the exact `/orders` list-row mapping is a documented `micro_live`-soak verification.

## Implementation Steps

- [x] `contracts/enums.py` + `contracts/capabilities.py` + `config/settings.py` (+ `.env.example`).
- [x] `adapters/streaming_pro/` — transport → mapping → models → errors → adapter → heartbeat →
  reconciler → runtime.
- [x] Wire `core/stage.py` + `core/router.py` + `api/deps.py` + `api/main.py`.
- [x] `docker-compose.streaming.yml`.
- [x] Tests ≥90% (respx) + stage-matrix + conftest reset; full gate 3.11+3.12 + pip-audit.
- [x] Docs (ROADMAP, CLAUDE.md, knowledge capability/decision-log, safety playbook) + PR + umbrella pin.

## File Changes

**New:** `src/quant_execution_engine/adapters/streaming_pro/{__init__,transport,mapping,models,errors,
adapter,heartbeat,reconciler,runtime}.py`, `docker-compose.streaming.yml`, `tests/unit/adapters/
streaming_pro/test_*.py`, this plan.
**Modified:** `contracts/enums.py`, `contracts/capabilities.py`, `config/settings.py`, `core/stage.py`,
`core/router.py`, `api/deps.py`, `api/main.py`, `.env.example`, `tests/test_core_stage_matrix.py`,
`tests/conftest.py`, `docs/plans/ROADMAP.md`, `CLAUDE.md`, `.claude/knowledge/*`, the order-routing
safety playbook.

## Success Criteria

- [ ] `resolve_adapter` routes `streaming_pro`: sim→sim, paper READ→real/sim, micro_live→real or typed
  `StageRejected`, live→rejected. Capability lookup returns the 2 new rows (`adapter_installed=True`).
- [ ] Adapter (respx): SET→`/order/place/set`, TFEX→`/order/place/tfex` with the right body + `X-API-Key`,
  **no PIN in the body**; cancel hits `/order/cancel` per-market; heartbeat trips/recovers; reconciler
  emits ack/fill actions.
- [ ] ruff + mypy strict + pytest **≥90% on `src/`** on **3.11 and 3.12**; pip-audit clean.
- [ ] `docker compose … -f docker-compose.streaming.yml config` valid; broker-free default unchanged.
- [ ] PR CI + Security green; umbrella `quant-execution-engine` pin advances; **`live` gated; no real order**.

## Completion Notes

### What shipped (2026-06-16)

The third real broker — `streaming_pro` — composes the `settrade-streaming-api` bridge over plain
`httpx`, mirroring `LiberatorAdapter`, with **no frozen-contract change**:

- **`adapters/streaming_pro/`** (9 modules): `transport` (httpx + `X-API-Key` + redacting logs, 4xx →
  structured rejection / 5xx → typed breaker food), `mapping` (pure `NormalizedOrder` → bridge body,
  uppercase-enum pass-through, **Decimal-as-string price**, **no PIN**), `models` (`BridgePlace` +
  tolerant `VenueOrderRow`), `errors`, `adapter` (the 7 frozen methods + `heartbeat` + the SET-cancel
  `ext_order_no` `/orders` lookup), `heartbeat` (`/session/status` probe → breaker → mass-cancel),
  `reconciler` v1 (lost-ack fuzzy match + watermark fills, `plan_actions` pure), `runtime` (singleton +
  `streaming_pro_enabled` predicate + workers).
- **Contracts/wiring:** `Broker.STREAMING_PRO`; 2 conservative `CapabilitySet` rows
  (`(MARKET, LIMIT) × DAY`, amend `cancel_replace`); 6 `EXECUTION_ENGINE_STREAMING_PRO_*` settings
  (**no PIN** — bridge-owned); `core/stage.py` + `core/router.py` + `api/deps.py` + `api/main.py`
  lifespan; `docker-compose.streaming.yml` (bridge + its `streaming-pro-redis`, mirrors liberator).
- **Tests:** `tests/unit/adapters/streaming_pro/` (place/cancel/reads/amend/heartbeat/reconciler/
  runtime/transport/mapping/models, respx-mocked) + 2 stage-matrix rows + conftest reset. **1034 tests,
  95.26% cov** on `src/` (3.11+3.12); ruff + mypy strict + pip-audit clean.

### Key decisions / deferrals

- **No PIN** in the engine (the bridge owns it) — a genuine simplification vs Liberator/Settrade.
- **Amend = `cancel_replace`** (the bridge's native amend is capture-pending) — router orchestrates.
- **Conservative capability** `(MARKET, LIMIT) × DAY` — expands as types are live-verified.
- **Deferred (documented):** the exact `/orders` list-row field mapping (verified in a `micro_live`
  soak); TFEX positions read (the `seosd` portfolio endpoint wasn't captured → `[]`); the live
  `micro_live` soak (operator-driven — bridge deployed + owner mode + market hours). **`live` gated.**

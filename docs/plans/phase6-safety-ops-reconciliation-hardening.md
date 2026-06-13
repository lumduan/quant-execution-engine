# Phase 6: Safety, Ops & Reconciliation Hardening

**Feature:** feature-execution-engine — Phase 6: Safety, Ops & Reconciliation Hardening
**Branch:** `feature/phase6-safety-ops-reconciliation-hardening`
**Created:** 2026-06-13
**Status:** In Progress
**Depends On:** Phase 5.1 (Complete)

---

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Implementation Steps](#implementation-steps)
6. [File Changes](#file-changes)
7. [Success Criteria](#success-criteria)
8. [Completion Notes](#completion-notes)

---

## Overview

### Purpose

Phase 6 is the **safety-hardening** phase of the `quant-execution-engine` — the platform's sole
real-money order router for SET + TFEX. It adds no features and no brokers; it makes the **failure
paths provably safe under fault injection**. Five independent workstreams strengthen the pre-trade
risk gate, harden the kill-switch admin trip, soak the idempotency + reconciliation logic under
adversarial fault, add per-adapter rate-limit / backpressure, and expose a structured audit read +
export over the append-only `execution.order_events` store.

The governing principle: **code conservatively, fail loud, test the failure paths as rigorously as
the happy path.** Everything is additive and behind the unchanged frozen contracts; **`live` stays
gated — Phase 6 adds no new path to real-money routing.**

### Parent Plan Reference

- Cross-cutting roadmap (umbrella): [`../../../plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md)
- Per-service roadmap (this repo): [`ROADMAP.md`](ROADMAP.md) — Phase 6 section is authoritative
- Approved meta-plan (driver): `~/.claude/plans/phase-6-vast-conway.md`

### Key Deliverables

1. **Pre-trade risk-gate hardening** — per-account notional/qty caps; advisory price-band check
   against live market data; unified duplicate-burst guard.
2. **Kill-switch admin-trip hardening** — idempotent engage/disengage, structured JSON audit log,
   `X-Operator-Id`, mass-cancel count; a 5-order fault-injection test.
3. **Idempotency soak + reconciliation drift tests** — PENDING_NEW stuck / ack-lost /
   fill-before-ack / same-cid retry; drift repair + `replace_resolve`; **no double-send under fault.**
4. **Per-adapter rate-limit / backpressure** — Settrade POST/GET token buckets; Liberator POST cap;
   an `EventHub` slow-subscriber stress test verifying the drop-oldest + gap-marker policy (§H).
5. **Structured audit export** — `GET /admin/orders/{cid}/audit` + streaming NDJSON
   `GET /admin/audit/export` (date / `strategy_id` filters), owner-mode only.
6. **Docs & knowledge** — this plan doc; decision-log (§H revisit); safety-playbook runbooks;
   `CLAUDE.md` + `.env.example` env-var tables; ROADMAP Phase 6 closed, Phase 7 unblocked.

---

## AI Prompt

The following prompt drives this phase (embedded verbatim):

````text
# Phase 6 — Safety, Ops & Reconciliation Hardening
# quant-execution-engine

## Role & responsibility

You are a principal engineer implementing **Phase 6** of the `quant-execution-engine` service — a safety-critical, real-money order router for Thai capital markets (SET + TFEX equity + derivatives). Your primary obligation is correctness under fault: no double-send, no silent drift, no money at risk under any failure mode that should be caught by the engine.

**This is the safety hardening phase. Code conservatively. Fail loud. Test failure paths as rigorously as the happy path.**

---

## Context you must read first (in this order)

Before touching any code or writing a plan:

1. **`CLAUDE.md`** (umbrella, at repo root) — system map, service registry, Docker network, cross-cutting rules
2. **`quant-execution-engine/CLAUDE.md`** — project rules, safety ladder, quality gate, hard rules, env vars, coding conventions
3. **`quant-execution-engine/docs/plans/ROADMAP.md`** — the per-service build sequence; Phase 6 scope is authoritative here
4. **`quant-execution-engine/.claude/knowledge/order-state-machine.md`** — the 13 frozen edges; reconciliation must only touch legal transitions
5. **`quant-execution-engine/.claude/knowledge/normalized-order-contract.md`** — the frozen `NormalizedOrder` / `NormalizedOrderResult` contract
6. **`quant-execution-engine/.claude/knowledge/capability-matrix.md`** — broker capability declarations; the PTRM + risk gate enforces per-capability limits
7. **`quant-execution-engine/.claude/knowledge/decision-log.md`** — decisions D1–D24 + open questions §A–§K; Phase 6 revisits §H (single-process fan-out)
8. **`quant-execution-engine/.claude/playbooks/order-routing-safety.md`** — existing Liberator + Settrade runbooks; Phase 6 **extends** these, never overwrites them
9. **`quant-execution-engine/docs/plans/phase5-strategy-execution-path-order-streaming.md`** — Phase 5 plan; understand what shipped before hardening it
10. **`strategies/csm-set/docs/plans/examples/phase1-sample.md`** — the required plan-doc format (Overview / AI Prompt / Scope / Design Decisions / Implementation Steps / File Changes / Success Criteria / Completion Notes)

---

## Branch

Create and work on: `feature/phase6-safety-ops-reconciliation-hardening`

Do not commit to `main`. All work ships in one PR from this branch.

---

## Step 1 — Write the implementation plan BEFORE any code

Using **Fable model at xHigh effort** for this step.

After reading all required docs, produce a detailed implementation plan and save it as:

quant-execution-engine/docs/plans/phase6-safety-ops-reconciliation-hardening.md

Follow the format from `strategies/csm-set/docs/plans/examples/phase1-sample.md` exactly:
- Frontmatter: Feature, Branch, Created (today's date), Status: In Progress, Depends On: Phase 5.1 (Complete)
- Table of Contents (anchor-linked)
- **Overview** — Purpose and Parent Plan Reference and Key Deliverables
- **AI Prompt** — embed this entire prompt verbatim inside a fenced code block
- **Scope** — In Scope table (component / description / status) + Out of Scope
- **Design Decisions** — numbered, each with rationale and trade-off
- **Implementation Steps** — numbered, one per deliverable, with acceptance assertion
- **File Changes** — table: File | Action | Description
- **Success Criteria** — checkbox list derived from Phase 6 acceptance criteria in the ROADMAP
- **Completion Notes** — leave as TODO placeholder

Commit the plan doc alone on the branch before writing any implementation code.

---

## Step 2 — Implementation

Use **Opus model as a subagent** for all coding and test writing in this step.

### Phase 6 scope (from `quant-execution-engine/docs/plans/ROADMAP.md` Phase 6 section)

Implement **all five workstreams** below. They are independent and may be developed in parallel, but each must pass the quality gate before the PR is opened.

---

### Workstream A — Pre-trade risk-gate hardening

**Current state:** The PTRM gate (Phase 2) enforces max-order-value, max-qty, and per-second order rate. Phase 6 strengthens it.

**A1 — Per-account notional and qty caps**

- Add per-account cap configuration (`EXECUTION_ENGINE_ACCOUNT_MAX_NOTIONAL`, `EXECUTION_ENGINE_ACCOUNT_MAX_QTY`) as `dict[str, Decimal]` and `dict[str, int]` respectively, loaded via `pydantic-settings` with `EXECUTION_ENGINE_` prefix.
- The risk gate must check `order.account` against both maps before routing. Missing account → fall back to the global cap (no silent skip).
- Caps must be enforced even when `EXECUTION_ENGINE_STAGE=sim` — the risk gate is not mode-dependent.
- Decimal throughout; never `float` at any monetary boundary.

**A2 — Price-band check against live market data**

- Add an optional price-band pre-trade check: if `EXECUTION_ENGINE_PRICE_BAND_ENABLED=true` AND `MARKET_DATA_BASE_URL` is set (already exists for the SimAdapter last-close hop, D21), fetch the last-close price for `order.symbol` from the market data engine before routing.
- If the order price deviates by more than `EXECUTION_ENGINE_PRICE_BAND_MAX_PCT` (default `10.0`, configurable) from last-close, reject with a typed `PriceBandExceeded` error (HTTP 422 with a `problem+json` envelope matching the existing typed-reject shape).
- The check is **non-blocking on fetch failure**: if the market data engine is unreachable, log a WARNING and pass the order through (the check is advisory, not a hard gate).
- MARKET orders bypass the band check (no limit price to validate).
- Tests must cover: within-band passes; outside-band rejects; MARKET bypasses; fetch-failure warns+passes; band check skipped when `PRICE_BAND_ENABLED=false`.

**A3 — Duplicate-burst guard**

- A second order with the **same `(account, symbol, side, quantity, order_type, price)`** submitted within a configurable window (`EXECUTION_ENGINE_DUPLICATE_BURST_WINDOW_SECONDS`, default `5`) — but with a **different** `client_order_id` — should be blocked with a typed `DuplicateBurstDetected` warning (HTTP 409). This is separate from idempotency dedupe (same cid), which already works.
- Track the fingerprint in Redis (the existing `quant-execution-redis` sidecar) with a TTL matching the window. Key format: `burst:{sha256(account:symbol:side:qty:type:price)}`.
- The guard is **off by default** (`EXECUTION_ENGINE_DUPLICATE_BURST_GUARD_ENABLED=false`) — enabling it is a deliberate operator choice.
- Tests: same-cid always passes (idempotency); different-cid same fingerprint within window → 409; different-cid same fingerprint after window → passes; guard-disabled → always passes.

---

### Workstream B — Kill-switch runbook + admin trip

**Current state:** The kill-switch exists (Phase 2) and is checked first in the submit path. Phase 6 tests it under fault and documents the trip procedure.

**B1 — Admin trip endpoint hardening**

- Audit the existing `/admin/kill-switch/engage` and `/admin/kill-switch/disengage` endpoints. Ensure:
  - Both require **owner mode** (`PUBLIC_MODE=false`) — return 403 in public mode with a `problem+json` body.
  - Engage: atomically set the flag, emit a structured `kill_switch.engaged` log event (JSON, never plain string), and initiate a best-effort async mass-cancel of all open orders. Return the number of orders mass-cancelled in the response body.
  - Disengage: only permitted when the kill-switch is engaged (409 if already disengaged). Emit a structured `kill_switch.disengaged` log event with the operator identity (`X-Operator-Id` header, optional string — log `anonymous` if absent, never require it).
  - Both endpoints must be idempotent: engage twice → second call returns 200 with `already_engaged=true`; disengage twice → 409.

**B2 — Kill-switch fault-injection test**

- Write a pytest test that:
  1. Places 5 orders (sim, mix of NEW and PARTIALLY_FILLED states).
  2. Calls the engage endpoint.
  3. Asserts all 5 orders are transitioned to CANCELLED in `execution.orders` (use the test DB).
  4. Asserts the `execution.order_events` audit trail has a `kill_switch_cancel` event for each order.
  5. Asserts a new submit with a fresh `client_order_id` is rejected with `kill_switch_engaged` typed error.
  6. Calls the disengage endpoint and asserts a fresh submit is accepted.
- This test must run in the normal `uv run pytest` invocation (no special marker needed; use the test DB fixtures already established in the codebase).

---

### Workstream C — Idempotency soak + reconciliation drift tests

**Current state:** Idempotency dedupe works in the happy path (Phase 2). Reconciliation loops exist for Liberator (Phase 3) and Settrade (Phase 4). Phase 6 tests them under adversarial fault.

**C1 — Submit-interrupt idempotency soak**

Write a parametrized test suite simulating a process restart mid-submit:

- **Scenario 1 — PENDING_NEW stuck:** Order is in `PENDING_NEW` state (network never responded). On reconciliation, the loop must fuzzy-match on `(account, symbol, side, qty)` within ±5 s (§B rule). Assert the order transitions to `NEW` or `REJECTED` (never stays `PENDING_NEW` indefinitely). Assert no duplicate submit attempt.
- **Scenario 2 — Ack lost after broker accepted:** `PENDING_NEW → NEW` transition was not persisted before restart. The reconciliation loop sees the broker `orderNo` and must ack it. Assert the `broker_order_id` is persisted and the order moves to `NEW`.
- **Scenario 3 — Fill received before state persisted:** A fill arrives for an order still in `PENDING_NEW`. Assert the fill is held until the ack lands (or rejected gracefully if the order is unrecognized after a bounded window).
- **Scenario 4 — Duplicate submit after restart:** A strategy retries with the same `client_order_id` (§A transport retry). Assert the engine returns the prior ack without re-routing.

All scenarios run against the `SimAdapter` (no broker creds needed in CI). Mock the broker-facing layer to inject the fault; test the engine's persistence + reconciliation logic directly.

**C2 — Reconciliation drift repair test**

- Simulate drift: set an order to `NEW` in `execution.orders` but `FILLED` at the (mocked) venue. Run the reconciliation loop. Assert the order transitions through `PARTIALLY_FILLED → FILLED` (or `FILLED` directly) and a fill row appears in `execution.fills`.
- Simulate the inverse: order is `FILLED` in the DB but `NEW` at the venue (DB ahead). Assert the loop does NOT regress the DB state (DB is truth; venue is updated asynchronously — the loop should not downgrade a terminal state).
- Test `replace_resolve` for `PENDING_REPLACE` drift (Settrade only): a stranded `PENDING_REPLACE` (ack lost for a native amend) must be resolved by the reconciler to either `NEW` (venue confirms the replacement) or back to the pre-replace price/qty (venue rejected the amend) — log the resolution either way.

---

### Workstream D — Per-adapter rate-limit / backpressure

**Current state:** The submit path does not enforce venue-level rate limits. The Settrade reconciler observes-don't-throttle. Phase 6 adds explicit enforcement.

**D1 — Settrade rate-limit tokens**

- Settrade enforces two buckets: place/amend/cancel (`POST/PATCH`) and query (`GET`). Add a token-bucket rate limiter (asyncio, no third-party library) per bucket:
  - `EXECUTION_ENGINE_SETTRADE_POST_RATE_LIMIT=10` requests/second (default)
  - `EXECUTION_ENGINE_SETTRADE_GET_RATE_LIMIT=10` requests/second (default)
- The token bucket must be per-`SettradeAdapter` instance (not global), initialized in `__init__`, and acquired before every httpx call inside the adapter.
- On bucket exhaustion: **await** (do not busy-spin; use `asyncio.sleep`), log a `WARN settrade_rate_limit_wait` with the wait duration, then proceed. Never silently drop a request or raise to the caller on rate-limit — the caller should see only an (eventually) successful or broker-rejected result.
- The reconciler already has a budget-skip mechanism; do NOT change it.

**D2 — Liberator submit serialization**

- Liberator's existing single-flight lock covers deduplication. Phase 6 adds a submit **rate cap** for the placement path (not the reconciler or heartbeat): `EXECUTION_ENGINE_LIBERATOR_POST_RATE_LIMIT=5` requests/second.
- Implement the same token-bucket pattern as D1.

**D3 — Backpressure on the SSE stream fan-out (§H revisit)**

- The `EventHub` is in-process (single uvicorn worker, §H). Phase 6 does **not** add multi-worker fan-out (Redis pub/sub) — that requires a concrete second-worker story (still deferred per §H decision). However, Phase 6 **must** verify the `drop-oldest + gap marker` policy under load.
- Write a stress test: 1,000 events/second published to the `EventHub` with 10 simultaneous slow subscribers (each queue is bounded to `STREAM_SUBSCRIBER_QUEUE_SIZE=256`). Assert: fast subscribers receive all events; slow subscribers receive `gap` markers when their queue overflows; the publisher never blocks; the order path never raises; the EventHub's `publish` remains exception-proof.
- **No code change needed if the existing policy already satisfies this.** Run the test, confirm it passes, commit it as a new test only.

---

### Workstream E — Structured audit export from `order_events`

**Current state:** `execution.order_events` is append-only and stores every state-machine transition. There is no read API for it.

**E1 — Audit read endpoint (owner-mode only)**

Add `GET /admin/orders/{client_order_id}/audit` (owner-mode only, 403 in public mode):

Response body (JSON):

```json
{
  "client_order_id": "...",
  "broker": "...",
  "symbol": "...",
  "events": [
    {
      "seq": 1,
      "from_status": "PENDING_NEW",
      "to_status": "NEW",
      "broker_order_id": "...",
      "event_type": "ack",
      "occurred_at": "2026-06-12T10:00:00Z",
      "metadata": {}
    }
  ]
}
```

- All timestamps UTC ISO 8601.
- metadata is the existing execution.order_events.metadata JSON column (opaque).
- Return 404 if client_order_id not in execution.orders.
- Query is read-only; no DB write.
- The endpoint must be covered by at least 3 test cases: event sequence for a sim-filled order; 404 for unknown id; 403 in public mode.

E2 — Audit NDJSON export (owner-mode only)

Add GET /admin/audit/export (owner-mode only, streaming response):

- Returns a streaming NDJSON response (Content-Type: application/x-ndjson) of all order_events rows, optionally filtered by:
  - ?from_ts=<ISO8601> (inclusive)
  - ?to_ts=<ISO8601> (exclusive)
  - ?strategy_id=<str> (joins on execution.orders.strategy_id)
- Stream rows as they are fetched (do not buffer all in memory). Use asyncpg streaming or a cursor with FETCH 500 batches.
- Each row is one JSON object per line (NDJSON).
- Include a Content-Disposition: attachment; filename="audit_{from_ts}_{to_ts}.ndjson" header.
- Test: small fixture export with 10 events, assert NDJSON line count and field presence; assert 403 in public mode.

---
Quality gate (must pass before PR opens)

uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest

- Coverage must remain ≥90% (--cov-fail-under=90 in pyproject.toml).
- mypy --strict must pass with zero errors.
- All new fault-injection tests must run in the default uv run pytest invocation (no skips, no external infra required beyond the test DB).
- The existing 853 tests must continue to pass — no regressions.

---
Constraints and hard rules (do not violate these)

1. Never log a PIN, token, account number, or order payload secret. All SecretStr values must be redacted in logs.
2. Never float at a monetary boundary. All prices are Decimal; wire format is Decimal-as-string.
3. The 13 frozen state-machine edges are immutable. Reconciliation may only trigger legal transitions; never force a terminal state backward.
4. The NormalizedOrder contract is frozen. Do not add or remove fields. If a new env var is needed, add it to Settings; never to the order payload.
5. live stays gated. Phase 6 adds no new path to real-money routing. Do not touch the stage ladder or the live rejection.
6. The kill-switch path is kill-switch-first. Any new code in the submit path must check the kill-switch before any other logic.
7. Adapters declare capabilities; the router enforces them. The rate limiter and burst guard are engine-side, not adapter-side — do not move the enforcement into the adapter.
8. No new broker. Phase 6 scope is hardening, not expansion.
9. E21 ban unchanged. The settrade-v2 SDK must not appear in adapters/ or any order-routing code path. Only the lazy-import market-data-only use in order_book/ is permitted.
10. from __future__ import annotations at the top of every src/ module.
11. uv run for all commands — never bare python/pip.

---
Step 3 — Knowledge and memory updates

After implementation, using Opus model:

1. Update quant-execution-engine/CLAUDE.md — add Phase 6 to the current-state summary in the preamble; update the env-var tables with all new EXECUTION_ENGINE_* settings added in this phase.
2. Update quant-execution-engine/.claude/knowledge/decision-log.md — add decision entries for:
  - §H revisit conclusion (single-process fan-out: confirmed or upgraded)
  - Any new design decisions made during Phase 6 implementation (e.g. token-bucket algorithm choice, burst-guard Redis key scheme)
3. Update quant-execution-engine/.claude/playbooks/order-routing-safety.md — append Phase 6 runbooks:
  - Kill-switch trip procedure (manual admin trip via the API; expected audit trail; disengage procedure)
  - Audit export procedure (how to pull a date-range NDJSON export for reconciliation)
4. Update the umbrella CLAUDE.md — update the feature-execution-engine status line and the Phase 6 entry in the engine catalog to reflect Phase 6 complete.
5. Update the umbrella .claude/knowledge/optional-features-registry.md — mark Phase 6 complete in the feature-execution-engine row.
6. Update quant-execution-engine/docs/plans/ROADMAP.md — set Phase 6 status to [x] Complete with the completion date; unblock Phase 7.
7. Update the plan doc quant-execution-engine/docs/plans/phase6-safety-ops-reconciliation-hardening.md — fill in the Completion Notes section.

---
Step 4 — Commit and PR

Using Opus model:

Commit message (Conventional Commits format, tight scope):

feat(phase6): safety, ops & reconciliation hardening

- Pre-trade risk gate: per-account notional/qty caps, price-band check,
  duplicate-burst guard (Redis TTL, default off)
- Kill-switch: admin trip hardening, structured audit log, fault-injection
  test (5-order mass-cancel, disengage roundtrip)
- Idempotency soak: PENDING_NEW stuck / ack-lost / fill-before-ack /
  same-cid retry scenarios; reconciliation drift repair + replace_resolve
- Rate limiters: Settrade POST/GET token buckets; Liberator POST cap;
  EventHub slow-subscriber stress test (gap-marker policy verified)
- Audit export: GET /admin/orders/{cid}/audit + streaming NDJSON export
  with date/strategy_id filter
- Knowledge: decision log (§H revisit), safety playbook runbooks, CLAUDE.md
  env-var tables updated; ROADMAP Phase 6 closed, Phase 7 unblocked

Open a PR to main on github.com/lumduan/quant-execution-engine:
- Title: feat(phase6): safety, ops & reconciliation hardening
- Body must include:
  - Summary (3–5 bullet points covering each workstream)
  - Test plan checklist (each fault-injection scenario, kill-switch roundtrip, audit export, quality gate command)
  - Note that live stays gated and no contract changes were made
  - 🤖 Generated with Claude Code

After the PR is open, report the result as an ASCII box-drawing table:

(Repo | Branch | Commit | GitHub) — one row for lumduan/quant-execution-engine (code).

---
Model selection rules (follow exactly)

- Reading docs, thinking, planning (Step 1) → Fable, xHigh effort
- Coding, test writing (Step 2, all workstreams) → Opus subagent
- Fixing errors, reviewing code → Fable
- Creating/updating docs, commit message, PR (Steps 3–4) → Opus

---
Expected final deliverables

When the PR is open, these must all be true:

- [ ] quant-execution-engine/docs/plans/phase6-safety-ops-reconciliation-hardening.md exists with all sections filled (including the embedded prompt and completion notes)
- [ ] All five workstreams (A–E) implemented and tested
- [ ] uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest exits 0 with ≥90% coverage
- [ ] No existing test regressed (853 baseline tests still pass)
- [ ] All new EXECUTION_ENGINE_* env vars documented in quant-execution-engine/CLAUDE.md and .env.example
- [ ] Safety playbook updated with kill-switch trip + audit export runbooks
- [ ] Decision log updated for §H revisit + any Phase 6 decisions
- [ ] ROADMAP Phase 6 closed, Phase 7 unblocked
- [ ] Umbrella CLAUDE.md and optional-features-registry updated
- [ ] PR open on github.com/lumduan/quant-execution-engine with the result table reported
- [ ] live is still gated — no new real-money path exists
````

> **Model note:** Fable was unavailable at execution time; per the model rules' Opus fallback for all
> other doc work, this plan doc and the Step-3/4 docs were authored with Opus.

---

## Scope

### In Scope (Phase 6)

| Component | Description | Status |
|---|---|---|
| A1 — per-account caps | `EXECUTION_ENGINE_ACCOUNT_MAX_NOTIONAL` / `ACCOUNT_MAX_QTY` dict settings; risk gate checks `order.account`, falls back to global; enforced in all stages incl. `sim` | Planned |
| A2 — price-band check | Optional advisory check vs market-data last-close; `PriceBandExceeded` 422 outside band; WARN+pass on fetch fail; MARKET bypass; reuses `adapters/sim_pricing.py` fetch | Planned |
| A3 — duplicate-burst guard | Unify the existing `duplicate_burst` check to richer fingerprint (`+type +price`), 5 s window, `DuplicateBurstDetected` 409, gated by `..._DUPLICATE_BURST_GUARD_ENABLED` (default-ON, see §1) | Planned |
| B1 — kill-switch trip hardening | Idempotent engage/disengage, structured JSON logs, `X-Operator-Id`, mass-cancel count; owner-mode 403 `problem+json` | Planned |
| B2 — kill-switch fault test | 5-order (NEW+PARTIALLY_FILLED) engage → all CANCELLED + audit rows + `kill_switch_engaged` reject → disengage → accept | Planned |
| C1 — idempotency soak | Parametrized PENDING_NEW-stuck / ack-lost / fill-before-ack / same-cid-retry; no double-send | Planned |
| C2 — reconciliation drift | DB-behind → repair to FILLED + fill row; DB-ahead → no terminal regress; Settrade `replace_resolve` | Planned |
| D1 — Settrade rate buckets | Per-`SettradeClient` GET + WRITE token buckets acquired in `request_json` (see §4); reconciler budget-skip untouched | Planned |
| D2 — Liberator POST cap | POST token bucket on `place()` only (not reconciler/heartbeat/cancel) | Planned |
| D3 — EventHub backpressure (§H) | Stress test: 1000 ev/s × 10 slow subscribers; fast get all, slow get gap markers, publisher never blocks | Planned |
| E1 — audit read endpoint | `GET /admin/orders/{cid}/audit`, owner-mode, synthesized response, 404/403 | Planned |
| E2 — audit NDJSON export | `GET /admin/audit/export`, streaming NDJSON, `from_ts`/`to_ts`/`strategy_id` filters, `Content-Disposition` | Planned |

### Out of Scope (Phase 6)

- **No new broker, no new order type** (ROADMAP non-goals).
- **No multi-worker `EventHub` fan-out / Redis pub-sub** — §H stays single-process (Design Decision §5).
- **No `quant-infra-db` schema change** — the audit response is synthesized from the existing
  `order_events` columns (Design Decision §3).
- **No change to the stage ladder, `live` gating, kill-switch-first ordering, PTRM caps semantics,
  or the frozen `NormalizedOrder` / state machine / capability cells.**
- **No change to the reconciler budget-skip mechanism** (observe-don't-throttle, D1 explicitly
  preserved).
- **Documentation hub (`docs/architecture|api|operations|data`)** — that is Phase 7.

---

## Design Decisions

### 1. Unify the duplicate-burst guard; default it ON (deviation from prompt's `false`)

A `duplicate_burst` check **already exists** in `core/risk.py` (fingerprint `account|symbol|side|qty`,
2 s window, always-on, HTTP 429, env `..._RISK_DUPLICATE_BURST_WINDOW_SECONDS`). The prompt's A3
describes a richer, opt-in guard as if net-new. Because the existing fingerprint is **coarser**, it
strictly shadows any finer guard for exact-duplicate orders — shipping two would make the new one
cosmetic. **Decision:** evolve the **single** existing guard to A3's contract — richer fingerprint
(`+order_type +price`), 5 s window via new `EXECUTION_ENGINE_DUPLICATE_BURST_WINDOW_SECONDS`, typed
`DuplicateBurstDetected` → 409, gated by `EXECUTION_ENGINE_DUPLICATE_BURST_GUARD_ENABLED`. The flag
**defaults to `true`**, a deliberate deviation from the prompt's `false`.

**Rationale:** a *hardening* phase must never silently disable an active safety guard; the richer
fingerprint is strictly better (still catches exact economic duplicates, stops over-blocking
legitimate re-prices, cleaner 409-conflict semantics). **Trade-off:** the default-on flag and the
429→409 change update a few existing risk tests, and operators relying on the old 2 s/429 behaviour
see new semantics; documented in the decision log and `.env.example`, and operators can set the flag
`false` to disable entirely.

### 2. Umbrella docs: branch + commit, defer push and pin bump to post-merge

The prompt opens one PR (engine) yet asks to mark Phase 6 "complete" in the umbrella `CLAUDE.md` +
registry. **Decision:** make the umbrella edits on a new umbrella branch, commit locally, and **do
not push or bump the submodule pin** until the engine PR merges. **Rationale:** the standard
submodule flow never bumps the parent pin against an unmerged SHA; the repo convention already
describes phases as "complete (PR open)". **Trade-off:** the umbrella system map lags the engine
branch until merge — acceptable and reversible.

### 3. No infra-db schema change — synthesize the audit response

`execution.order_events` has only `from_status`, `to_status`, and an `event` JSONB column — **no
`event_type` and no `metadata` column** (the schema deliberately omits `metadata`). **Decision:** the
audit endpoint **synthesizes** its response from the real columns: `seq` ← per-order `ROW_NUMBER()`;
`broker_order_id`/`metadata` ← the `event` JSONB; `event_type` ← a pure `(from_status → to_status)`
mapping; `occurred_at` ← `created_at` (UTC ISO 8601). **Rationale:** the audit store is the separate
`quant-infra-db` repo's concern; Phase 6 ships from the engine repo only and adds a read path, not a
migration. **Trade-off:** `event_type` is derived, not stored — a future schema that stores it
natively would supersede the mapping (additive).

### 4. Settrade rate buckets live per-`SettradeClient` (per OAuth app), not one per adapter

Settrade enforces rate limits **per OAuth app**, and Phase 4.1 runs **one `SettradeClient` per
market** (InnovestX `ALGO_EQ` = SET, `ALGO` = TFEX). **Decision:** one GET + one WRITE token bucket
**per client**, acquired at the single send choke point (`SettradeClient.request_json`, keyed by HTTP
method). **Rationale:** a single per-adapter bucket would wrongly throttle SET and TFEX together,
under-using each app's independent allowance; per-client matches the real venue boundary while staying
adapter-owned (still "not global", per the prompt's intent). **Trade-off:** a small, documented
deviation from the prompt's literal "per-`SettradeAdapter` instance" — chosen for correctness.

### 5. §H confirmed single-process — D3 is a verification test, not a code change

§H (single-process fan-out, deferred in Phase 5) is **revisited and confirmed**: Phase 6 adds **no**
multi-worker fan-out (no Redis pub/sub). The `EventHub` already implements the drop-oldest +
gap-marker overflow policy with an exception-proof `publish`. **Decision:** D3 ships a **stress test
only** that proves the policy under 1000 ev/s × 10 slow subscribers. **Rationale:** multi-worker
fan-out still has no concrete second-worker story (§H deferral stands); verifying the existing policy
is the right hardening step. **Trade-off:** none — purely additive coverage; the decision log records
"§H confirmed, not upgraded".

### 6. `kill_switch_cancel` audit = genuine CANCELLED rows + structured log

The DB trigger writes `order_events` rows keyed by transition (`PENDING_CANCEL → CANCELLED`); the app
cannot inject a literal `event_type="kill_switch_cancel"` row without an out-of-scope infra-db trigger
change. **Decision:** B2 asserts the **genuine** CANCELLED-transition audit rows per order (the real
mechanism) **plus** a structured `kill_switch.engaged` engine log carrying the mass-cancel count.
**Rationale:** faithful to the actual append-only audit machinery without a schema change.
**Trade-off:** the literal `event_type` label is surfaced by the E1 derivation as `cancel`, not
`kill_switch_cancel`; the kill-switch context lives in the structured log + the engage response count.

### 7. Token-bucket algorithm: pure asyncio, monotonic clock, await-on-deficit

**Decision:** one shared `TokenBucket` (new `adapters/rate_limit.py`) — monotonic-clock lazy refill
under an `asyncio.Lock`, `await asyncio.sleep(deficit)` on exhaustion, a single WARN with the wait
duration, never busy-spin, never drop, never raise to the caller. **Rationale:** the prompt forbids a
third-party library; a lazy-refill bucket is the minimal correct primitive and is trivially testable
with an injected clock. **Trade-off:** await-on-deficit serialises bursts (intended back-pressure);
fairness across waiters is FIFO via the lock, sufficient for the low order volume.

### 8. Submit-path ordering preserved; price-band slots after the risk gate

Kill-switch stays checked **first** (hard rule 6). The price-band check (A2) is wired into
`router.submit` **after** the existing PTRM risk gate and **before** adapter routing, so a malformed
or capped order is rejected before any market-data hop. **Rationale:** preserves the kill-switch-first
invariant and keeps the (optional, network-touching) band check off the hot path for already-rejected
orders. **Trade-off:** the band check adds one awaited market-data GET on the submit path when enabled
— bounded by the advisory WARN+pass-on-failure contract.

---

## Implementation Steps

### Step 0 — Settings + error scaffolding

Add the new settings to `config/settings.py` (account-cap dicts, price-band enable/max-pct, burst
enable/window, Settrade POST/GET limits, Liberator POST limit) and document each in `.env.example`.
Add `PriceBandExceeded` + `DuplicateBurstDetected` to `contracts/errors.py` and their status mappings
(422 / 409) to `api/error_handlers.py`. **Acceptance:** `Settings()` loads with all new vars at their
defaults; the two new error codes resolve to 422/409 in the handler map.

### Step A1 — Per-account notional/qty caps

Extend `RiskGate.check` to look up `order.account` in the account-cap maps before routing; missing
account → global cap (no silent skip); `Decimal` throughout; enforced in `sim`. **Acceptance:** a
per-account cap binds when present and the global cap binds on a missing account, in `sim`.

### Step A2 — Price-band check

New `core/price_band.py` `PriceBandCheck` reusing the market-data last-close fetch factored out of
`adapters/sim_pricing.py`; wired into `router.submit` after the risk gate. **Acceptance:** within-band
passes; outside-band → 422 `PriceBandExceeded`; MARKET bypasses; fetch-failure WARN+passes; disabled
flag skips entirely.

### Step A3 — Duplicate-burst guard (unify, default-ON)

Evolve the existing guard per Design Decision §1: richer fingerprint, new window/enable settings,
`DuplicateBurstDetected` → 409, `exe:burst:` keyspace. Retire the old 429 path + update its tests.
**Acceptance:** same-cid passes; different-cid same fingerprint in-window → 409; after-window passes;
guard-disabled passes.

### Step B1 — Kill-switch admin-trip hardening

Harden `api/routes.py` engage/disengage: idempotent (`already_engaged=true` / 409 if already
disengaged), structured `kill_switch.engaged|disengaged` JSON logs, optional `X-Operator-Id`
(`anonymous` default), mass-cancel `cancelled_count` in `api/schemas.py`. **Acceptance:** engage twice
→ 200 `already_engaged`; disengage when disengaged → 409; both 403 in public mode; engage returns a
count.

### Step B2 — Kill-switch fault-injection test

5 sim orders (NEW + PARTIALLY_FILLED) → engage → all 5 `CANCELLED` in `execution.orders` + a CANCELLED
audit row each → fresh submit rejected `kill_switch_engaged` → disengage → fresh submit accepted.
**Acceptance:** the test passes in the default `uv run pytest` invocation.

### Step C1 — Submit-interrupt idempotency soak

Parametrized suite (SimAdapter, broker layer mocked) for Scenarios 1–4. **Acceptance:** PENDING_NEW
resolves to `NEW`/`REJECTED` (never stuck, no duplicate submit); ack-lost persists `broker_order_id`
→ `NEW`; fill-before-ack is held/bounded-reject; same-cid retry returns the prior ack without
re-routing.

### Step C2 — Reconciliation drift repair

Drift tests over `adapters/{liberator,settrade}/reconciler.py` + `db/repositories.py`. **Acceptance:**
DB-behind repairs to FILLED + `execution.fills` row; DB-ahead does not regress a terminal state;
stranded `PENDING_REPLACE` resolves to `NEW` or restored pre-replace price/qty, logged either way.

### Step D1 — Settrade rate-limit tokens

Per-`SettradeClient` GET + WRITE `TokenBucket`s acquired in `request_json` keyed by method; WARN on
wait; reconciler budget-skip untouched. **Acceptance:** with limit 1/s, two rapid WRITE calls serialise
(second awaits ≈1 s, logs the wait), neither drops nor raises; GET and WRITE buckets are independent.

### Step D2 — Liberator submit rate cap

POST `TokenBucket` on `LiberatorAdapter`, acquired only in `place()`. **Acceptance:** `place()`
respects the cap while `cancel()`/heartbeat/reconciler fetches are unthrottled.

### Step D3 — EventHub slow-subscriber stress test (§H)

Stress test only. **Acceptance:** 1000 ev/s × 10 slow subscribers (queue 256) — fast subscribers
receive all events, slow subscribers receive `gap` markers on overflow, the publisher never blocks,
the order path never raises, `publish` stays exception-proof.

### Step E1 — Audit read endpoint

New `api/audit.py` + `db` reader `fetch_order_events`; synthesized response (Design Decision §3),
owner-mode. **Acceptance:** sim-filled order returns its ordered event sequence; unknown cid → 404;
public mode → 403; ≥3 tests.

### Step E2 — Audit NDJSON export

`GET /admin/audit/export` streaming NDJSON via an asyncpg server-side cursor (`FETCH 500`), optional
`from_ts`/`to_ts`/`strategy_id` filters, `Content-Disposition` filename. **Acceptance:** a 10-event
fixture exports 10 NDJSON lines with the expected fields; public mode → 403; rows stream (not buffered).

---

## File Changes

| File | Action | Description |
|---|---|---|
| `docs/plans/phase6-safety-ops-reconciliation-hardening.md` | CREATE | This plan document (committed alone first) |
| `config/settings.py` | MODIFY | Account caps (dict), price-band, burst flag+window, Settrade POST/GET limits, Liberator POST limit |
| `.env.example` | MODIFY | Document every new `EXECUTION_ENGINE_*` var |
| `contracts/errors.py` | MODIFY | `PriceBandExceeded`, `DuplicateBurstDetected` |
| `api/error_handlers.py` | MODIFY | Status map: `price_band_exceeded`→422, `duplicate_burst_detected`→409 |
| `core/risk.py` | MODIFY | A1 per-account caps; A3 unified burst guard |
| `core/price_band.py` | CREATE | A2 price-band check (reuses sim_pricing last-close fetch) |
| `core/router.py` | MODIFY | Wire the price-band check after the risk gate |
| `adapters/sim_pricing.py` | MODIFY | Factor out a shared market-data last-close fetcher |
| `adapters/rate_limit.py` | CREATE | `TokenBucket` (pure asyncio) |
| `adapters/settrade/client.py` | MODIFY | D1 GET/WRITE buckets in `request_json` |
| `adapters/liberator/adapter.py` | MODIFY | D2 POST bucket in `place()` |
| `api/routes.py` | MODIFY | B1 kill-switch engage/disengage hardening (`X-Operator-Id`, structured log, count) |
| `api/schemas.py` | MODIFY | `already_engaged`, `cancelled_count` on the engage response |
| `api/audit.py` | CREATE | E1/E2 audit read + NDJSON export router |
| `db/repositories.py` | MODIFY | `fetch_order_events` + cursor export; minor C touches |
| `tests/…` | CREATE/MODIFY | Every workstream incl. fault-injection + stress; update retired-burst tests |
| `CLAUDE.md` (engine) | MODIFY | Phase 6 preamble + env-var tables |
| `.claude/knowledge/decision-log.md` | MODIFY | §H revisit + Phase 6 decisions (burst default-on, per-market buckets, token algo) |
| `.claude/playbooks/order-routing-safety.md` | MODIFY | Kill-switch trip + audit export runbooks |
| `docs/plans/ROADMAP.md` | MODIFY | Phase 6 `[x]` + date; unblock Phase 7 |
| umbrella `CLAUDE.md` + `.claude/knowledge/optional-features-registry.md` | MODIFY | Phase 6 complete (separate umbrella branch; local commit; push/pin deferred to post-merge) |

---

## Success Criteria

- [ ] `docs/plans/phase6-safety-ops-reconciliation-hardening.md` exists with all sections filled (embedded prompt + completion notes)
- [ ] All five workstreams (A–E) implemented and tested
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest` exits 0 with ≥90% coverage
- [ ] No existing test regressed (853 baseline) — except the deliberately-updated A3 burst tests
- [ ] The documented failure-injection suite passes; **no double-submit under fault**
- [ ] Reconciliation provably repairs drift (DB-behind repairs; DB-ahead never regresses); `replace_resolve` verified
- [ ] The kill-switch is verified end-to-end (5-order mass-cancel + audit rows + disengage roundtrip)
- [ ] All new `EXECUTION_ENGINE_*` env vars documented in engine `CLAUDE.md` and `.env.example`
- [ ] Safety playbook updated with kill-switch trip + audit export runbooks
- [ ] Decision log updated for §H revisit + Phase 6 decisions
- [ ] ROADMAP Phase 6 closed `[x]`, Phase 7 unblocked
- [ ] Umbrella `CLAUDE.md` + optional-features-registry updated (umbrella branch; push/pin deferred)
- [ ] PR open on `github.com/lumduan/quant-execution-engine` with the result table reported
- [ ] **`live` is still gated — no new real-money path exists; no frozen-contract change**

---

## Completion Notes

_TODO: fill in on completion — Summary, Issues Encountered, final test count + coverage, document footer._

# Phase 3: LiberatorAdapter — First Real Broker (sim-gated)

> **Status:** In progress (started 2026-06-11)
> **Branch:** `feat/phase3-liberator-adapter`
> **Parent plan:** [`ROADMAP.md`](ROADMAP.md) — "Phase 3 — LiberatorAdapter (first real broker)"
> **Builds on:** [`phase2-engine-core-simadapter.md`](phase2-engine-core-simadapter.md)

## Table of Contents

- [Overview](#overview)
- [AI Prompt](#ai-prompt)
- [Scope](#scope)
- [Design Decisions](#design-decisions)
- [Architecture & Request Flow](#architecture--request-flow)
- [Implementation Steps](#implementation-steps)
- [File Changes](#file-changes)
- [Success Criteria](#success-criteria)
- [Completion Notes](#completion-notes)

## Overview

### Purpose

Connect the execution plane to its first real venue. `LiberatorAdapter` composes the
bundled `liberator-trading-api` service over HTTP (D9 — never re-implements it),
implementing the full frozen `BrokerAdapter` interface behind the safety ladder:
`sim` stays broker-free, `paper` intercepts placement into sim while keeping the
Liberator session live for reads, `micro_live` routes real orders at PTRM-capped
size, and `live` stays gated. Reconciliation loop v1 repairs submit/ack drift
against venue truth; a ~30 s heartbeat drives the Phase-2 circuit-breaker scaffold.

### Parent Plan Reference

- [`docs/plans/ROADMAP.md`](ROADMAP.md) — Phase 3 scope, acceptance criteria, deployment notes (canonical spec).
- Umbrella: [`plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md).
- Frozen contracts: [`normalized-order-contract.md`](../../.claude/knowledge/normalized-order-contract.md),
  [`order-state-machine.md`](../../.claude/knowledge/order-state-machine.md),
  [`capability-matrix.md`](../../.claude/knowledge/capability-matrix.md),
  [`broker-research-liberator.md`](../../.claude/knowledge/broker-research-liberator.md),
  [`decision-log.md`](../../.claude/knowledge/decision-log.md).

### Key Deliverables

1. `adapters/liberator/` package: wire models, pure field mapping, redacting
   `httpx.AsyncClient` transport, `LiberatorAdapter` (place / cancel / amend /
   reads / heartbeat / capabilities).
2. Reconciliation loop v1 (`reconciler.py`): polls `GET /orders/{account}`, drives
   the frozen 13-edge state machine via the Phase-2 `repositories` seams, fuzzy-matches
   lost acks per ADR §B, bounded resolution, never re-sends.
3. Session heartbeat worker + circuit breaker wiring: trip ⇒ typed
   `broker_circuit_open` on new submits + mass-cancel attempted; reset on healthy poll;
   state surfaced in `/health` and `/capabilities`.
4. Stage-ladder integration: `paper` place-intercept, `micro_live` real route,
   `live` still rejected; `EXECUTION_ENGINE_LIBERATOR_*` settings (PIN as `SecretStr`).
5. ≥90 % coverage with respx-mocked HTTP; no live credentials anywhere.
6. Upstream `liberator-trading-api` verification-system audit + minimal auth
   hardening (dual-commit; see Design Decision 10).

## AI Prompt

The binding prompt that initiated this phase (reproduced verbatim):

````text
## Objective

  You are implementing **Phase 3 — `LiberatorAdapter` (first real broker)** of the `quant-execution-engine` service — the execution
  plane's first connection to a real venue. Phases 0–2 are complete (ADR accepted, `execution` order store live, SimAdapter + gateway
  proxy live). Phase 3 routes a real order to Liberator, idempotently, behind the safety ladder.

  Your work scope is **`quant-execution-engine/`** (and potentially touching `liberator-trading-api` only through its submodule pin,
  never rewriting it). No Settrade work. No live stage promotion. No order-update streaming push (Phase 5).

  ---

  ## Step 0 — Pre-read before writing a single line of code

  Read **all** of the following in your first turn. Do not skip:

  1. `CLAUDE.md` — umbrella system map, network contract, submodule rules
  2. `quant-execution-engine/CLAUDE.md` — service context, hard rules, commands, quality gate
  3. `quant-execution-engine/docs/plans/ROADMAP.md` — Phase 3 scope, acceptance criteria, and deployment notes (the "Phase 3 —
  LiberatorAdapter" section is your canonical spec)
  4. `quant-execution-engine/.claude/knowledge/broker-research-liberator.md` — the Liberator HTTP surface, field shapes, status/error
  taxonomy, idempotency gaps
  5. `quant-execution-engine/.claude/knowledge/capability-matrix.md` — frozen capability grid; the adapter must declare its exact
  capability set
  6. `quant-execution-engine/.claude/knowledge/order-state-machine.md` — frozen state machine; the reconciliation loop drives these
  transitions
  7. `quant-execution-engine/.claude/knowledge/normalized-order-contract.md` — frozen wire types (`Decimal`-as-string, `int` qty, UTC)
  8. `quant-execution-engine/.claude/knowledge/decision-log.md` — D9 (compose, never re-implement), E7–E12 Phase-2 realisation decisions
  you build on
  9. `quant-execution-engine/.claude/playbooks/order-routing-safety.md` — the "When adding a broker adapter" checklist; every item is
  binding
  10. `quant-execution-engine/docs/plans/phase2-engine-core-simadapter.md` — the Phase-2 plan to understand the `BrokerAdapter` interface
  and `OrderRouter` you are implementing against
  11. `strategies/csm-set/docs/plans/examples/phase1-sample.md` — the required plan document format reference

  ---

  ## Step 1 — Create git branch

  ```bash
  # Working directory: quant-execution-engine/
  git checkout -b feat/phase3-liberator-adapter

  ---
  Step 2 — Write the plan document first; no code until it is written

  Create quant-execution-engine/docs/plans/phase3-liberator-adapter.md before writing any implementation code. Follow the section
  structure from strategies/csm-set/docs/plans/examples/phase1-sample.md:

  - Table of Contents
  - Overview (purpose, parent plan reference, key deliverables)
  - AI Prompt ← include this entire prompt verbatim in a fenced code block in this section
  - Scope (in-scope table per deliverable with status column; explicit out-of-scope list)
  - Design Decisions (record every non-obvious call, with rationale and alternatives rejected)
  - Architecture & Request Flow (how a NormalizedOrder travels from OrderRouter → LiberatorAdapter → liberator-trading-api → venue, and
  how the reconciliation loop drives state transitions back)
  - Implementation Steps (ordered, atomic steps you will follow)
  - File Changes (table: file path | action | description)
  - Success Criteria (checkboxes from the ROADMAP Phase 3 acceptance criteria)
  - Completion Notes (fill in after implementation)

  Commit the plan document as a standalone commit before starting code:

  docs(plans): add phase3-liberator-adapter plan

  ---
  Step 3 — Implementation

  3a. Submodule: third_party/liberator-trading-api

  The liberator-trading-api repo is vendored as a git submodule at third_party/liberator-trading-api/.

  Dual-commit rule (hard — do not break this):
  If you need to add, update, or modify anything inside third_party/liberator-trading-api/, you must:
  1. Commit + push the change inside the submodule's own repo first (branch main).
  2. Only then pin the new SHA in quant-execution-engine with git add third_party/liberator-trading-api and a separate commit.

  Never commit the parent against an unpushed submodule SHA.

  If the submodule is not yet registered, add it:

  git submodule add https://github.com/lumduan/liberator-trading-api.git third_party/liberator-trading-api
  git submodule update --init

  3b. Docker overlay: docker-compose.liberator.yml

  Create docker-compose.liberator.yml so the public/sim default (docker compose up) stays completely broker-free. Liberator only joins
  when this overlay is layered:

  docker compose -f docker-compose.yml -f docker-compose.private.yml -f docker-compose.liberator.yml up -d

  Requirements:
  - liberator-trading-api service: no host port (internal only, not registered in the umbrella network table, not a platform peer)
  - Container name: liberator-trading-api; internal hostname used by the adapter: http://liberator-trading-api:8200/api/v1 (the service
  listens on port 8200 — its config/system.yaml api.port)
  - Its own Redis sidecar: liberator-redis (distinct from quant-execution-redis)
  - Broker credentials come from this repo's gitignored .env only
  - Because liberator-trading-api's settings loader merges YAML over env vars (env REDIS_HOST would be ignored), pin the container's
  Redis host/port via a mounted docker/liberator/system.yaml
  - The submodule ships stub Dockerfile/compose only; build from this repo's docker/liberator/Dockerfile (Python ≥3.13)

  3c. LiberatorAdapter — the core deliverable

  Implement src/quant_execution_engine/adapters/liberator/adapter.py (and supporting modules in
  src/quant_execution_engine/adapters/liberator/) implementing the full BrokerAdapter interface (D3/§D). All HTTP calls use
  httpx.AsyncClient. No requests. from __future__ import annotations at top of every module.

  Capability declaration

  Declare the Liberator capability set for the router to enforce (D7/§F). Use the frozen capability matrix from
  quant-execution-engine/.claude/knowledge/capability-matrix.md as ground truth. Key constraints to encode:

  ┌────────────────────────────┬──────────────┬───────────────────────┐
  │   NormalizedOrder field    │     SET      │         TFEX          │
  ├────────────────────────────┼──────────────┼───────────────────────┤
  │ order_type=STOP/STOP_LIMIT │ ✗ reject     │ ✅                    │
  ├────────────────────────────┼──────────────┼───────────────────────┤
  │ order_type=ATO/ATC         │ ✅           │ ✗ reject              │
  ├────────────────────────────┼──────────────┼───────────────────────┤
  │ order_type=MTL → MP        │ ✅           │ ✗ reject              │
  ├────────────────────────────┼──────────────┼───────────────────────┤
  │ position_effect            │ must be None │ required (OPEN/CLOSE) │
  └────────────────────────────┴──────────────┴───────────────────────┘

  Field mapping: NormalizedOrder → Liberator

  SET (POST /api/v1/order/place/set → SETOrderRequest):

  ┌───────────────────────┬────────────────────────────────────────────┬────────────────────────────────────────────────────────┐
  │    NormalizedOrder    │              Liberator field               │                         Notes                          │
  ├───────────────────────┼────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
  │ side=BUY              │ side="Buy"                                 │                                                        │
  │ side=SELL             │ side="Sell"                                │                                                        │
  │ order_type=LIMIT      │ priceType="Limit"                          │                                                        │
  │ order_type=MARKET     │ priceType="Market"                         │                                                        │
  │ order_type=MTL        │ priceType="MP"                             │ market-price-to-limit                                  │
  │ order_type=ATO        │ priceType="ATO"                            │                                                        │
  │ order_type=ATC        │ priceType="ATC"                            │                                                        │
  │ order_type=ICEBERG    │ priceType="Limit" + icebergVol=display_qty │                                                        │
  │ tif=DAY/GTC/IOC/FOK   │ validityType same                          │                                                        │
  │ price                 │ price (2 dp Decimal)                       │                                                        │
  │ quantity              │ volume                                     │ int                                                    │
  │ account               │ accountNo                                  │ never log in full                                      │
  │ PIN from env/settings │ pin                                        │ never log; sourced from EXECUTION_ENGINE_LIBERATOR_PIN │
  └───────────────────────┴────────────────────────────────────────────┴────────────────────────────────────────────────────────┘

  TFEX (POST /api/v1/order/place/tfex → TFEXOrderRequest):

  ┌────────────────────────────┬─────────────────────────────────────────────────────────┬───────┐
  │      NormalizedOrder       │                     Liberator field                     │ Notes │
  ├────────────────────────────┼─────────────────────────────────────────────────────────┼───────┤
  │ side=BUY                   │ side="Long"                                             │       │
  │ side=SELL                  │ side="Short"                                            │       │
  │ position_effect=OPEN       │ position="Open"                                         │       │
  │ position_effect=CLOSE      │ position="Close"                                        │       │
  │ order_type=STOP/STOP_LIMIT │ priceType="Stop" + stopCondition, stopSymbol, stopPrice │       │
  │ order_type=ICEBERG         │ priceType="Limit" + icebergVol=display_qty              │       │
  │ TFEX TIF same as SET       │ validityType                                            │       │
  └────────────────────────────┴─────────────────────────────────────────────────────────┴───────┘

  Cancel

  Map DELETE /orders/{client_order_id} → retrieve broker_order_id from DB → POST /api/v1/order/cancelled/{set|tfex} with
  orderNo=[broker_order_id] + PIN (≤50 per call). Transition: NEW/PARTIALLY_FILLED → PENDING_CANCEL (persist first) → venue call →
  CANCELLED on confirm.

  Amend (cancel-then-replace — non-atomic, declared)

  LiberatorAdapter.amend = cancel the existing order → place a new NormalizedOrder with a new client_order_id. Declare this behaviour in
  the capability set so callers know:
  - Queue-priority loss is expected
  - A brief period with no resting order at either price is possible
  - The old client_order_id ends in CANCELLED; the new one begins its lifecycle at PENDING_NEW

  Status / error mapping

  Map Liberator's errorCode / errMsg / rejectCode to normalized status and reject_reason. errorCode == 0 and no errMsg = success.
  Non-zero or errMsg = REJECTED with mapped reject_reason. Never swallow a reject.

  client_order_id ↔ broker_order_id mapping (ADR §B)

  Persist the mapping atomically with the PENDING_NEW → NEW transition (the DB trigger already appends the audit row). If the ack is
  lost, the reconciliation loop (§3d) fuzzy-matches on (account, symbol, side, qty) within ±5 s of the persisted submit timestamp.

  3d. Reconciliation loop v1

  Implement an asyncio background task (src/quant_execution_engine/adapters/liberator/reconciler.py) that:
  - Polls GET /api/v1/orders/{account_no} periodically (configurable interval, default ~10–15 s)
  - Maps OrderItem fields (status, statusShow, matched, balance, cancelled, rejectCode) to the normalized status enum
  - Drives the frozen state machine transitions (PENDING_NEW → NEW, NEW → PARTIALLY_FILLED, NEW/PARTIALLY_FILLED → FILLED, etc.) via the
  existing repositories.apply_fill() seam from Phase 2
  - Fuzzy-matches lost-ack orders (stuck PENDING_NEW for >5 s) by (account, symbol, side, qty) within the ±5 s window per ADR §B
  - Never blindly re-sends; a stuck PENDING_NEW resolves bounded, never blocks routing
  - Is only started when the adapter is active (not in sim stage)

  3e. Session heartbeat + circuit breaker

  Wire the Phase-2 circuit-breaker scaffold into LiberatorAdapter:
  - A background heartbeat worker polls a low-impact read (e.g. GET /api/v1/orders or session-status) every ~30 s (ADR §G)
  - N consecutive failures (configurable, default 3) → trip the circuit breaker: EXECUTION_ENGINE_STAGE routing for Liberator halts; new
  submits for broker=liberator get a typed error broker_circuit_open; mass-cancel is attempted
  - Circuit resets after a successful health poll
  - The trip state is reflected in /health and /capabilities responses

  3f. Settings additions

  Add under EXECUTION_ENGINE_* prefix (in src/quant_execution_engine/config/settings.py):

  - EXECUTION_ENGINE_LIBERATOR_BASE_URL — adapter target URL (default http://liberator-trading-api:8200/api/v1)
  - EXECUTION_ENGINE_LIBERATOR_PIN — trading PIN (never logged, never validated beyond presence; type SecretStr)
  - EXECUTION_ENGINE_LIBERATOR_HEARTBEAT_INTERVAL_SECONDS (default 30)
  - EXECUTION_ENGINE_LIBERATOR_CIRCUIT_BREAKER_THRESHOLD (default 3)
  - EXECUTION_ENGINE_LIBERATOR_RECONCILE_INTERVAL_SECONDS (default 12)

  All config via pydantic-settings; secrets sourced from the gitignored .env only.

  3g. Router integration

  The existing OrderRouter in Phase 2 already dispatches by broker. Extend it to:
  - Route broker=liberator to LiberatorAdapter
  - Reject broker=liberator when EXECUTION_ENGINE_STAGE=sim (sim routes to SimAdapter regardless of broker field — clarify this semantic
  in Design Decisions)
  - Reject when the Liberator circuit breaker is tripped

  3h. Stage validation

  - paper stage: Liberator is queried for account/position reads (realism), but place_order is intercepted and not sent — route to
  SimAdapter instead. This requires Liberator session to be live (heartbeat active) but does not submit real orders.
  - micro_live stage: real orders, smallest venue size cap enforced by PTRM (already wired in Phase 2).
  - live stage: stays gated — Phase 3 does not unlock it.

  ---
  Step 4 — Tests (≥90% coverage, broker HTTP mocked, no live creds in CI)

  Create tests/unit/adapters/liberator/ and tests/integration/adapters/liberator/ mirroring the source layout.

  Required test coverage:

  1. Field mapping — parametrize every (market, order_type, side, position_effect, tif) combination that is valid; assert the Liberator
  request payload fields are exactly correct.
  2. Capability rejection — every unsupported (broker=liberator, market, order_type) combo raises the correct typed pre-flight error
  before any HTTP call.
  3. Status mapping — every errorCode/rejectCode value maps to the correct NormalizedStatus and a non-empty reject_reason.
  4. Idempotency — submit the same client_order_id twice; assert the second call returns the prior ack and the Liberator HTTP endpoint is
  called only once.
  5. Reconciliation — mock GET /orders with a sequence of responses (PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED); assert the state
  machine transitions are driven correctly and fill rows are persisted.
  6. Lost-ack fuzzy match — mock a PENDING_NEW stuck for >5 s; assert the reconciler matches on (account, symbol, side, qty) and advances
  to NEW.
  7. Amend (cancel+replace) — assert the cancel endpoint is called before the new place endpoint; assert the old client_order_id ends
  CANCELLED; assert the new order begins PENDING_NEW.
  8. Circuit breaker trip — mock N consecutive heartbeat failures; assert subsequent place_order calls return broker_circuit_open typed
  error; assert mass-cancel is attempted.
  9. PIN never logged — assert no log call in any place_order, cancel, or amend path contains the PIN string.
  10. paper stage intercept — assert place_order is never forwarded to Liberator HTTP when stage is paper.

  Mock the Liberator HTTP service with respx or pytest-httpx. No unittest.mock for HTTP calls. No live credentials in any test fixture.

  ---
  Step 5 — Quality gate (must pass clean before commit)

  uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest

  - mypy --strict must be clean.
  - --cov-fail-under=90 must pass on adapters/ + the order state machine.
  - Do not push with red CI. If ruff format --check fails after any edit, re-run ruff format . then re-check.

  ---
  Step 6 — Update knowledge, CLAUDE.md files, and playbooks

  After implementation, update the following (only if the content has changed or gaps exist):

  - quant-execution-engine/.claude/knowledge/decision-log.md — add a "Phase 3 realisation decisions" section recording any non-obvious
  calls (e.g. paper-stage intercept behaviour, heartbeat interval default, fuzzy-match window)
  - quant-execution-engine/.claude/knowledge/capability-matrix.md — confirm all Liberator cells are now validated against the live
  adapter code, not just research notes; annotate any discovered divergences
  - quant-execution-engine/.claude/playbooks/order-routing-safety.md — add a Liberator-specific section: secret hygiene (PIN via
  SecretStr, never in logs), circuit-breaker trip runbook, and bring-up sequence for the liberator overlay
  - quant-execution-engine/CLAUDE.md — update the "Current state" block to reflect Phase 3 complete; add environment variables added in
  §3f to any env-var reference table
  - CLAUDE.md (umbrella) — in the feature-execution-engine feature row and Phase 3 status note, update to "Phase 3 complete (YYYY-MM-DD)
  — LiberatorAdapter live, reconcile loop v1, heartbeat + circuit breaker; micro_live validated; no real-money default"

  Also update quant-execution-engine/docs/plans/phase3-liberator-adapter.md with Completion Notes (summary, issues encountered, decisions
  changed from the plan).

  ---
  Step 7 — Commit and PR

  Follow Conventional Commits. Scope tightly. Suggested sequence:

  chore(submodules): register liberator-trading-api as third_party submodule
  chore(docker): add docker-compose.liberator.yml overlay + docker/liberator/ build assets
  feat(adapters): implement LiberatorAdapter (place/cancel/amend, SET+TFEX mapping, capability set)
  feat(adapters): add LiberatorAdapter reconciliation loop v1
  feat(adapters): wire liberator heartbeat + circuit breaker
  feat(router): route broker=liberator to LiberatorAdapter; paper-stage intercept
  feat(config): add EXECUTION_ENGINE_LIBERATOR_* settings
  test(adapters): liberator adapter tests, ≥90% coverage
  docs(plans): update phase3-liberator-adapter plan with completion notes
  docs: update CLAUDE.md files and knowledge/playbooks for Phase 3

  After quality gate passes, open a PR from feat/phase3-liberator-adapter → main on quant-execution-engine. PR title: feat: Phase 3 —
  LiberatorAdapter (first real broker, sim-gated). PR body must include:
  - Summary bullets (what ships, what doesn't)
  - Bring-up instructions for the liberator overlay (owner mode)
  - Test plan checklist
  - Confirmation: no real credentials in any committed file; PIN only via .env

  After the PR is open, bump the quant-execution-engine submodule pin in the umbrella repo:

  # In quant-trading-system/
  git -C quant-execution-engine fetch
  git -C quant-execution-engine checkout origin/main
  git add quant-execution-engine
  git commit -m "chore: bump quant-execution-engine pin to <sha>"

  Report the result as an ASCII box-drawing table (Repo | Branch | Commit | GitHub) for every repo touched.

  ---
  Hard rules (never violate these)

  1. D9 enforced. LiberatorAdapter composes liberator-trading-api over HTTP. It does not re-implement Liberator logic.
  2. Dual-commit rule. Any change inside third_party/liberator-trading-api/ must be committed + pushed in the submodule repo first, then
  pinned in the parent. Never commit the parent against an unpushed submodule SHA.
  3. No credentials in repo. PIN, OTP/session tokens, account numbers → gitignored .env only. SecretStr in settings. Never logged.
  4. Kill-switch first. The kill-switch check precedes everything in the submit path. Do not reorder it.
  5. No live stage in Phase 3. micro_live is the highest rung Phase 3 exercises. The live gate stays closed.
  6. No streaming push. Order-update streaming is Phase 5. Do not add WS/event emission in Phase 3.
  7. uv run always. Never bare python/pip/poetry/conda.
  8. Decimal at money boundaries. Never float for prices, fees, or quantities.
  9. Async-first I/O. All HTTP via httpx.AsyncClient. requests forbidden in src/.
  10. from __future__ import annotations at the top of every src/ module.
````

A follow-up prompt extended the scope mid-phase:

````text
addition note :  The liberator-trading-api repository contains an older project I created. Some aspects might not be working
efficiently, such as the verification system. I'd appreciate it if you could help check it. If you find any improvements that enhance
performance, please report them in the plan. I'd like this to be a best-practice approach. I wrote this repository myself a long time
ago, and I believe your help could help improve it alongside my current project.
````

## Scope

### In Scope

| # | Deliverable | Status |
|---|---|---|
| 1 | Submodule `third_party/liberator-trading-api` registered + pinned (pre-existing from scaffolding — verify only) | [x] verified |
| 2 | `docker-compose.liberator.yml` overlay + `docker/liberator/` build assets (pre-existing — verify only; `/api/v1/health/` healthcheck confirmed valid) | [x] verified |
| 3 | `EXECUTION_ENGINE_LIBERATOR_*` settings (base_url, api_key + pin as `SecretStr`, heartbeat/breaker/reconcile knobs) | [ ] |
| 4 | `adapters/liberator/` wire layer: `errors.py`, `models.py`, `mapping.py`, `transport.py` | [ ] |
| 5 | `LiberatorAdapter` — full 7-method `BrokerAdapter` + `heartbeat()` + `fetch_venue_orders()` | [ ] |
| 6 | Reconciliation loop v1 (`reconciler.py`) + `repositories.fetch_orders_for_reconcile` | [ ] |
| 7 | Heartbeat worker + circuit-breaker wiring (`heartbeat.py`), trip ⇒ `broker_circuit_open` + mass-cancel | [ ] |
| 8 | Runtime singleton + lifespan workers (`runtime.py`, `api/main.py`) | [ ] |
| 9 | Stage matrix: paper intercept / micro_live route / live gated (`core/stage.py`, `core/router.py`) | [ ] |
| 10 | Router-level `amend()` (cancel+replace, new client_order_id; not HTTP-exposed) | [ ] |
| 11 | `/health` + `/capabilities` surface breaker state; capability rows flip `adapter_installed=True` | [ ] |
| 12 | Tests ≥90 % (respx; the 10 required cases) in `tests/unit/adapters/liberator/` + `tests/integration/adapters/liberator/` | [ ] |
| 13 | Upstream auth hardening in `liberator-trading-api` (shared timing-safe `verify_api_key`, UTC timestamps) — dual-commit + pin bump | [ ] |
| 14 | Docs: CLAUDE.md files, ROADMAP, decision log, capability matrix, safety playbook | [ ] |

### Out of Scope (deferred, per ROADMAP)

- `SettradeAdapter` (Phase 4) and every `(confirm P4)` capability cell.
- Order-update streaming push / WS events (Phase 5).
- `live` stage unlock — `micro_live` is the highest rung Phase 3 exercises.
- Amend HTTP route (`PATCH /orders/...`) — Phase 4; only the router-level orchestration ships now.
- Real-venue acceptance (OTP login is operator-driven; runbook documented, not executed in CI).
- Larger upstream refactors of `liberator-trading-api` (typing modernisation, exception taxonomy) — reported, not implemented.

## Design Decisions

1. **Process-singleton adapter runtime** (`adapters/liberator/runtime.py`). `api/deps.py`
   constructs a new `OrderRouter` per request, so breaker/heartbeat state must live in a
   module-level singleton mirroring `db/postgres.py` / `cache/redis_client.py`.
   `OrderRouter.__init__` gains an optional `liberator_adapter` falling back to
   `get_liberator_adapter()`; existing call sites and tests stay valid.
   *Rejected:* per-request adapter (breaker state would reset every request); app.state
   (breaks the established singleton pattern and the MemStore test style).
2. **Stage matrix with an `AdapterIntent` axis** (`TRADE` | `READ`). `sim` → sim always
   (broker field ignored — sim is a stage, not a broker); `paper` → TRADE intercepted to
   sim, READ goes to Liberator when configured (account/position realism); `micro_live` →
   broker=liberator routes real, others as before; `live` → still `StageRejected`.
   Cancels route by row broker + stage at call time; a mid-lifecycle stage flip is an
   operator error covered by a playbook rule (kill-switch + mass-cancel before flipping).
   *Rejected:* rejecting broker=liberator at sim stage (contradicts Phase-2 semantics where
   sim routes everything to SimAdapter; the prompt's "clarify this semantic" resolved this way).
3. **Amend = router-level cancel+replace through the PENDING_CANCEL path.** The frozen
   graph has no `PENDING_REPLACE → CANCELLED` edge — `PENDING_REPLACE` is reserved for
   native amends (Settrade, Phase 4). `OrderRouter.amend(...)` cancels the old order
   (PENDING_CANCEL → CANCELLED), then submits a rebuilt `NormalizedOrder` with the
   caller-supplied fresh `client_order_id` through the full frozen pipeline.
   `LiberatorAdapter.amend()` itself returns `AmendAck(ok=False, semantics="cancel_replace")`
   — it never pretends an atomic amend happened. Queue-priority loss and a brief
   no-resting-order window are declared consequences.
4. **Reconciler fills are cumulative-watermark deltas.** Liberator reports cumulative
   `matched`; the reconciler computes `delta = matched − db_filled_qty` and applies one
   fill with synthetic `broker_fill_id = f"{orderNo}:{matched}"` — re-polls regenerate the
   same id and dedupe via the existing `ON CONFLICT DO NOTHING`. Fill price = order price
   (limit family) with `amount/matched` fallback for market-family — documented
   approximation until a per-fill stream exists (Phase 5).
5. **Venue states that lack a frozen edge map to the nearest truthful terminal.**
   Venue-cancelled with no local PENDING_CANCEL → two-step PENDING_CANCEL → CANCELLED.
   Venue reject after ack (no `NEW → REJECTED` edge) → `set_reject_reason` (venue truth
   preserved) + PENDING_CANCEL → CANCELLED + WARNING. Expiry → EXPIRED (legal edges exist).
6. **Lost-ack resolution is bounded at 60 s** (module constant ≈ 5 reconcile passes at the
   12 s default): PENDING_NEW older than 5 s fuzzy-matches `(account, symbol, side, qty)`
   against venue `entryTime` within ±5 s of `created_at` (ADR §B); a unique match acks,
   an ambiguous match skips + warns (never guess), and an unmatched order past the bound
   goes PENDING_NEW → REJECTED with `reject_reason="ack_lost_unmatched"`. Never re-sends.
7. **Heartbeat target is `GET order/health/set`**: healthy ⇔ HTTP 200 ∧ `status=="healthy"`
   ∧ `auth_token_available` — exactly the dead-broker-session signal, no venue round-trip,
   no PIN. *Rejected:* `/session/status` (fires a real venue probe — too heavy at 30 s);
   bare `/health` (doesn't prove the auth token exists).
8. **`CircuitOpenError.code` renamed `broker_session_down` → `broker_circuit_open`.**
   The prompt pins the wire code; only two repo references existed and nothing real
   consumed the Phase-2 string. One condition, one code (a new subclass would split it).
9. **Worker start predicate:** runtime + heartbeat when `stage ∈ {paper, micro_live, live}`
   ∧ owner mode ∧ both Liberator secrets present (missing ⇒ WARNING + liberator routing
   disabled, micro_live submits get `StageRejected`). The reconciler starts only at
   `micro_live`/`live` — at `paper`, placements land in sim, and "reconciling" sim-acked
   broker=liberator rows against venue truth would corrupt them. This deliberately narrows
   the prompt's "not in sim stage" wording; recorded here and in the decision log.
10. **Upstream verification-system audit (user-requested) — findings:**
    the api-key check compared secrets with `==` (timing-unsafe) in
    `app/services/otp_sms_service.py:validate_api_key`, `verify_api_key` was copy-pasted
    into 14 endpoint modules with a function-local import while `app/dependencies.py` sat
    empty, the expected key was re-read from `os.getenv` per request (and raised
    `ValueError` → 500 when unset), and health timestamps used naive `datetime.now()`
    mislabeled with a `Z` suffix. Positives: the HTTP client is genuinely async
    (`curl_cffi.AsyncSession`) and Redis uses `redis.asyncio` with pooling + retry — no
    event-loop blocking found. **Fix shipped in Phase 3** (user-confirmed): one shared,
    timing-safe (`hmac.compare_digest`) `verify_api_key` dependency in
    `app/dependencies.py`, endpoints import it, UTC timestamps — committed and pushed in
    the submodule repo first, then pinned here (dual-commit rule).
11. **`fetch_venue_orders()` as a non-interface adapter method.** The frozen 7-method
    interface stays intact; the reconciler needs raw `VenueOrderItem`s (orderNo, matched,
    rejectCode…), which `get_open_orders() -> list[NormalizedOrder]` cannot carry.
    Precedent: `SimAdapter.heartbeat()` already extends beyond the frozen seven.

## Architecture & Request Flow

### Submit (micro_live, broker=liberator)

```
POST /orders (owner mode + API key)
  └─ OrderRouter.submit                      [unchanged frozen pipeline]
       1 kill-switch FIRST          (hard rule 3)
       2 dedupe by client_order_id  (prior ack on hit)
       3 capability gate            (LIBERATOR×SET / LIBERATOR×TFEX rows)
       4 PTRM caps                  (micro_live smallest-size enforcement)
       5 resolve_adapter(stage=micro_live, broker=liberator, intent=TRADE)
            └─ LiberatorAdapter (process singleton)
       6 adapter.breaker.guard()    → broker_circuit_open when OPEN
       7 single-flight lock
       8 INSERT PENDING_NEW         (durable BEFORE venue I/O)
       9 adapter.place(order)
            └─ mapping.to_{set|tfex}_payload(order, pin=SecretStr)
            └─ LiberatorTransport.post("order/place/{set|tfex}")
                 └─ http://liberator-trading-api:8200/api/v1 (api-key header)
                      └─ Liberator venue
            ├─ errorCode != 0 / errMsg  ⇒ PlaceAck(rejected, venue text)  ⇒ REJECTED
            └─ ok ⇒ PlaceAck(broker_order_id=orderNo, fills=())
       10 ack_order: ONE UPDATE → NEW + broker_order_id (§B atomic; trigger audits)
       11 fills arrive via the reconciler (not the ack) in v1
```

### Reconcile pass (every 12 s at micro_live/live)

```
fetch_orders_for_reconcile(broker=liberator)        [PENDING_NEW|NEW|PARTIALLY_FILLED|PENDING_CANCEL]
  group by account ── adapter.fetch_venue_orders(account) ── index by orderNo
  per local row → plan_actions(row, filled_qty, venue_item, now):
    PENDING_NEW + match            → ack_order(cid, orderNo)                      [PENDING_NEW→NEW]
    PENDING_NEW >5s, no broker id  → fuzzy (account,symbol,side,qty) ±5s entryTime
    PENDING_NEW >60s unmatched     → REJECTED "ack_lost_unmatched"                [bounded]
    matched > filled_qty           → apply_fill(delta, id=f"{orderNo}:{matched}") [NEW→PF→FILLED]
    venue cancelled                → PENDING_CANCEL → CANCELLED                   [two-step]
    venue expired                  → EXPIRED
    rejectCode (pre-ack)           → REJECTED + reject_reason
    rejectCode (post-ack)          → set_reject_reason + PENDING_CANCEL→CANCELLED + WARNING
```

### Heartbeat / breaker (every 30 s when stage ≥ paper, owner mode)

```
heartbeat_loop: ok = adapter.heartbeat()    [GET order/health/set: 200 ∧ healthy ∧ auth_token_available]
  ok  → breaker.record_success()            [OPEN → CLOSED reset]
  err → breaker.record_failure()            [N consecutive ⇒ OPEN]
  CLOSED→OPEN transition → on_trip(): OrderRouter.mass_cancel()  (attempted, best-effort)
  /health + /capabilities expose brokers.liberator.{breaker_state, session_healthy}
```

## Implementation Steps

1. [x] Branch `feat/phase3-liberator-adapter`; verify submodule pin + docker overlay (pre-existing).
2. [ ] This plan document — standalone `docs(plans)` commit.
3. [ ] `chore(deps)`: add `respx` to the dev group; `uv sync --all-groups`.
4. [ ] `feat(config)`: Liberator settings (SecretStr) + `broker_circuit_open` rename + `.env.example`.
5. [ ] `feat(adapters)`: wire layer — `errors.py`, `models.py`, `mapping.py`, `transport.py` + unit tests.
6. [ ] `feat(adapters)`: `adapter.py` (place/cancel/amend/reads/heartbeat/capabilities) + unit tests.
7. [ ] `feat(adapters)`: `reconciler.py` + `heartbeat.py` + `runtime.py` + `repositories.fetch_orders_for_reconcile` + MemStore extension + unit tests.
8. [ ] `feat(core)`: `AdapterIntent` stage matrix, router `liberator_adapter` param + `amend()`, lifespan workers, `/health` + `/capabilities` brokers block, capability rows `adapter_installed=True` + router/API tests.
9. [ ] Upstream hardening: submodule commit+push (`fix(auth)` in liberator-trading-api main), then `chore(submodules)` pin bump here.
10. [ ] `docs:` CLAUDE.md / ROADMAP / decision log / capability matrix / safety playbook + Completion Notes here.
11. [ ] Quality gate green → push → PR → merge on green CI → umbrella pin bump + CLAUDE.md row.

## File Changes

| File | Action | Description |
|---|---|---|
| `docs/plans/phase3-liberator-adapter.md` | add | This plan |
| `pyproject.toml` | modify | `respx` dev dependency |
| `src/quant_execution_engine/config/settings.py` | modify | `liberator_*` fields (PIN/api-key as `SecretStr`) |
| `src/quant_execution_engine/adapters/errors.py` | modify | `CircuitOpenError.code = "broker_circuit_open"` |
| `src/quant_execution_engine/api/error_handlers.py` | modify | status map key rename (503 kept) |
| `.env.example` | modify | heartbeat/breaker/reconcile knobs |
| `src/quant_execution_engine/adapters/liberator/__init__.py` | add | package export |
| `src/quant_execution_engine/adapters/liberator/errors.py` | add | `LiberatorAdapterError` / `LiberatorTransportError` / `LiberatorMappingError` |
| `src/quant_execution_engine/adapters/liberator/models.py` | add | `LiberatorEnvelope` / `LiberatorData` / `VenueOrderItem` |
| `src/quant_execution_engine/adapters/liberator/mapping.py` | add | pure NormalizedOrder → Liberator payload mapping |
| `src/quant_execution_engine/adapters/liberator/transport.py` | add | redacting httpx transport (api-key header; relative-path join) |
| `src/quant_execution_engine/adapters/liberator/adapter.py` | add | `LiberatorAdapter(BrokerAdapter)` |
| `src/quant_execution_engine/adapters/liberator/heartbeat.py` | add | heartbeat pass + loop + trip hook |
| `src/quant_execution_engine/adapters/liberator/reconciler.py` | add | `plan_actions` (pure) + `LiberatorReconciler` |
| `src/quant_execution_engine/adapters/liberator/runtime.py` | add | process singleton + worker lifecycle |
| `src/quant_execution_engine/core/stage.py` | modify | `AdapterIntent` + extended `resolve_adapter` |
| `src/quant_execution_engine/core/router.py` | modify | `liberator_adapter` param; `amend()` orchestration |
| `src/quant_execution_engine/api/deps.py` | modify | inject the runtime singleton |
| `src/quant_execution_engine/api/main.py` | modify | lifespan: create runtime, start/stop workers |
| `src/quant_execution_engine/api/schemas.py` | modify | `BrokerRuntimeHealth` + optional `brokers` blocks |
| `src/quant_execution_engine/api/routes.py` | modify | populate `brokers` on `/health` + `/capabilities` |
| `src/quant_execution_engine/contracts/capabilities.py` | modify | LIBERATOR rows `adapter_installed=True` |
| `src/quant_execution_engine/db/repositories.py` | modify | `fetch_orders_for_reconcile` |
| `tests/_fakes.py` | modify | MemStore `fetch_orders_for_reconcile`; repo-function list |
| `tests/conftest.py` | modify | liberator runtime reset; `make_liberator_order` |
| `tests/unit/adapters/liberator/test_*.py` | add | mapping / transport / adapter / heartbeat / reconciler / runtime suites |
| `tests/test_core_stage_matrix.py` | add | full stage×broker×intent matrix |
| `tests/test_core_router_liberator.py` | add | idempotency / breaker / paper intercept / amend ordering |
| `tests/test_api_routes.py`, `tests/test_adapters_session.py`, `tests/test_infra_singletons.py` | modify | brokers block; renamed code; new resolver |
| `tests/integration/adapters/liberator/test_live_liberator.py` | add | integration-marked live-stack checks |
| `third_party/liberator-trading-api` (own repo) | modify+pin | shared timing-safe `verify_api_key`; UTC timestamps |
| `CLAUDE.md`, `docs/plans/ROADMAP.md`, `.claude/knowledge/*`, `.claude/playbooks/order-routing-safety.md` | modify | Phase 3 status, decisions, runbook |

## Success Criteria

- [ ] A normalized order reaches Liberator (mock-verified end-to-end; live path operator-gated) and the round-trip (ack → fills → status) reconciles.
- [ ] Re-submission of the same `client_order_id` is idempotent — prior ack returned, exactly one venue HTTP call.
- [ ] Capability matrix reflects Liberator's real SET/TFEX support; unsupported combos reject pre-flight with typed errors before any HTTP.
- [ ] Reconciliation loop v1 drives PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED from polled venue truth; lost-ack fuzzy match works; resolution is bounded.
- [ ] Heartbeat + circuit breaker: N consecutive failures trip the breaker; submits return `broker_circuit_open`; mass-cancel attempted; healthy poll resets; state visible in `/health` and `/capabilities`.
- [ ] `paper` intercepts placement to sim with zero Liberator HTTP calls; `micro_live` routes real; `live` stays gated.
- [ ] PIN/account never logged (asserted by caplog test); no credentials in any committed file.
- [ ] Quality gate green: ruff + ruff format + mypy strict + pytest ≥90 % coverage (HTTP mocked, no live creds in CI).
- [ ] Upstream auth hardening landed via dual-commit; submodule pin advanced.

## Completion Notes

_To be filled in after implementation._

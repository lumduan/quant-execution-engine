# Phase 4: SettradeAdapter — Second Broker (SET + TFEX, native amend, OAuth)

> **Status:** Complete (2026-06-11)
> **Branch:** `feature/phase4-settrade-adapter`
> **Parent plan:** [`ROADMAP.md`](ROADMAP.md) — "Phase 4 — SettradeAdapter (second broker — proves the abstraction)"
> **Builds on:** [`phase3-liberator-adapter.md`](phase3-liberator-adapter.md)

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

Add the **second** real broker and prove the `BrokerAdapter` abstraction scales with **zero
contract change**. Settrade is architecturally distinct from Liberator in every dimension that
matters: it authenticates via **OAuth app-credentials** (`app_id`/`app_secret`/`app_code`),
not an OTP/PIN session; it supports **native order amendment** (a real venue amend endpoint,
not the cancel-then-replace hack Liberator forced); and it is a **cloud API** with no bundled
upstream service to compose. Phase 4 routes the *same* `NormalizedOrder` to either broker by
`broker`/account and proves capability divergences (native amend, SET+TFEX coverage, distinct
order/TIF enum sets) are enforced **up front**, never discovered at the venue.

Two operator-confirmed scope amendments supersede the original task prompt and the old ROADMAP
non-goal (recorded in the AI Prompt note and Design Decision 1):

1. **Full SET equity + TFEX derivatives** coverage — 100% of the `settrade-v2` investor order
   surface. The ROADMAP's "no SET market on Settrade" non-goal is **struck** (operator-directed).
2. **`PATCH /orders/{client_order_id}`** native-amend HTTP route ships — the Phase 3
   `router.amend` docstring already promises "the route lands Phase 4".

### Parent Plan Reference

- [`docs/plans/ROADMAP.md`](ROADMAP.md) — Phase 4 scope, acceptance, capability cells (canonical spec).
- Umbrella: [`plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md).
- Frozen contracts: [`normalized-order-contract.md`](../../.claude/knowledge/normalized-order-contract.md),
  [`order-state-machine.md`](../../.claude/knowledge/order-state-machine.md),
  [`capability-matrix.md`](../../.claude/knowledge/capability-matrix.md),
  [`broker-research-settrade.md`](../../.claude/knowledge/broker-research-settrade.md),
  [`decision-log.md`](../../.claude/knowledge/decision-log.md).
- Phase-3 precedent (structural template): [`phase3-liberator-adapter.md`](phase3-liberator-adapter.md).

### Key Deliverables

1. `adapters/settrade/` package: `errors.py`, `client.py` (OAuth transport — the only wire
   module), `models.py`, `mapping.py` (pure), `adapter.py`, `heartbeat.py`, `reconciler.py`,
   `runtime.py` — mirrors the actual `adapters/liberator/` names.
2. **Native amend through the frozen `PENDING_REPLACE → NEW` edge** — one-statement
   `replace_order` UPDATE setting status+price+quantity atomically; non-terminal restore on
   venue reject + typed `AmendRejected` (409).
3. **`PATCH /orders/{client_order_id}`** owner-mode route (native brokers amend in place; the
   cancel_replace branch returns the replacement cid).
4. Reconciler v1 with a **`replace_resolve`** action for stranded `PENDING_REPLACE`; OAuth
   token-liveness heartbeat + circuit breaker (mirrors Liberator); stage matrix
   (`paper` intercept / `micro_live` real / `live` gated).
5. `EXECUTION_ENGINE_SETTRADE_*` settings (credentials `SecretStr`, all optional); `cryptography>=42`
   for ECDSA P-256 login signing; **no compose overlay** (cloud API).
6. Capability rows pinned from official venue docs (replace every `(confirm P4)` cell); ≥90 %
   respx-mocked coverage; UAT-sandbox integration skeleton (no live creds in CI).

## AI Prompt

The binding prompt that initiated this phase is reproduced **verbatim** below.

> **Scope subsequently amended by the operator** (these supersede the prompt's wording, and are
> reflected throughout this document — see Design Decision 1): the TFEX-only non-goal is
> **struck** in favour of **full SET equity + TFEX derivatives** coverage, and a
> **`PATCH /orders/{client_order_id}` native-amend route** is added to scope.

````text
# Phase 4 — SettradeAdapter: Second Broker Implementation for quant-execution-engine

  ## Context & Mission

  You are implementing **Phase 4 — SettradeAdapter** in `quant-execution-engine/`, the platform's canonical order router (FastAPI, Python 3.11, host `:8400`). This is the second real
  broker adapter after `LiberatorAdapter` (Phase 3) and its primary purpose is to **prove the `BrokerAdapter` abstraction scales across brokers**. Settrade is architecturally
  distinct from Liberator: it uses OAuth (`app_id`/`app_secret`/`app_code`) rather than OTP/PIN, supports **native order amendment** (no cancel-then-replace hack), and is the gateway
  to SET equities and potentially derivatives for a different credential set.

  This task spans: planning → implementation → tests → documentation → knowledge updates → commit + PR.

  **Model selection rule (enforce throughout your work):**
  - Thinking, planning, architecture decisions → **Fable model, xHigh effort**
  - Coding, writing tests → **Opus as subagent**
  - Fixing errors, reviewing code → **Fable model**
  - Docs, commit messages, PR → **Opus**

  ---

  ## Step 0 — Read Before Writing Anything

  Read ALL of the following before creating any plan or code. Do not skip:

  1. `CLAUDE.md` (umbrella system map, ownership rules, ingestion contract)
  2. `quant-execution-engine/CLAUDE.md` (service rules, quality gates, hard rules, coding conventions)
  3. `quant-execution-engine/docs/plans/ROADMAP.md` (Phase 4 spec section, capability matrix, state machine, D1–D13)
  4. `quant-execution-engine/.claude/knowledge/broker-research-settrade.md` (Settrade API surface, auth flow, order types, amend mechanics)
  5. `quant-execution-engine/.claude/knowledge/broker-research-liberator.md` (pattern precedent)
  6. `quant-execution-engine/.claude/knowledge/capability-matrix.md` (what Phase 4 must fill in for Settrade rows)
  7. `quant-execution-engine/.claude/knowledge/normalized-order-contract.md` (frozen contract — do not modify)
  8. `quant-execution-engine/.claude/knowledge/order-state-machine.md` (frozen state machine)
  9. `quant-execution-engine/.claude/knowledge/decision-log.md` (prior decisions to respect)
  10. `quant-execution-engine/.claude/playbooks/order-routing-safety.md` (safety ladder, stage-flip rule, breaker trips)
  11. `quant-execution-engine/src/quant_execution_engine/adapters/liberator/` (full implementation — your pattern reference)
  12. `quant-execution-engine/src/quant_execution_engine/adapters/base.py` (or equivalent `BrokerAdapter` ABC)
  13. `strategies/csm-set/docs/plans/examples/phase1-sample.md` (plan file format reference — copy this structure exactly)
  14. `quant-execution-engine/pyproject.toml` (deps, coverage config, lint config)
  15. `quant-execution-engine/.env.example` (env var naming conventions)

  ---

  ## Step 1 — Create a Git Branch

  ```bash
  cd quant-execution-engine
  git switch main
  git pull
  git switch -c feature/phase4-settrade-adapter

  ---
  Step 2 — Write the Implementation Plan (Fable + xHigh effort)

  Using Fable model at xHigh effort, produce the full implementation plan BEFORE writing any code.

  Plan scope (answer all of these in the document):

  Architecture:
  - How does SettradeAdapter implement BrokerAdapter? Walk through each method: place_order, cancel_order, amend_order (native — not cancel-then-replace), get_order_status,
  heartbeat/session_keepalive, reconcile.
  - Settrade OAuth flow: token acquisition (app_id/app_secret/app_code), token refresh lifecycle, expiry handling, where the token is stored (in-process SecretStr, never logged).
  - Stage matrix for Settrade: paper behavior (intercept to sim, session live for reads?), micro_live (real route at PTRM cap), live (gated until explicitly unlocked — confirm gate
  condition).
  - Circuit breaker design: trip threshold, mass-cancel trigger, /health and /capabilities surface.
  - Reconciliation loop: interval, fuzzy match strategy for lost-ack (adapt §B from Liberator or supersede), bounded resolution window.
  - Native amend (PENDING_REPLACE state): how the router invokes it, what the state machine transition looks like, how it differs from Liberator's cancel-then-replace. Confirm this
  "pins the (confirm P4) cells" in the capability matrix.
  - Capability matrix rows: enumerate all (broker=settrade, market, order_type, tif) combinations that Phase 4 will support. Be explicit about what is deferred.
  - Settings: new EXECUTION_ENGINE_SETTRADE_* env vars (follow Liberator naming pattern, all SecretStr for credentials).
  - Docker compose changes: any new service dependency or overlay needed?
  - Error taxonomy: new SettradeError subtypes, how they map to the typed-rejection envelope.

  Risks & mitigations:
  - OAuth token expiry mid-session
  - Amend rejection at the venue (partial fill raced the amend)
  - Reconcile loop Settrade API rate limits
  - PENDING_REPLACE transition when kill-switch fires mid-amend

  Deferred scope (explicitly out of Phase 4):
  - State clearly what is NOT included (e.g., derivatives market via Settrade if out of scope, streaming order updates if deferred to Phase 5+).

  Test strategy:
  - Unit tests: mock HTTP, cover all state transitions including native amend, token refresh, circuit breaker trip, kill-switch override.
  - Integration test outline (can be skipped in CI with a marker, but must exist as a skeleton).
  - Coverage target: ≥90% on adapters/settrade/ and all touched router code.

  Plan file output:

  Save the plan as quant-execution-engine/docs/plans/phase4-settrade-adapter.md.

  Use the exact format from strategies/csm-set/docs/plans/examples/phase1-sample.md — include: phase title, date, status, objective, scope (in/out), architecture decisions,
  implementation steps (numbered, each with sub-tasks), test strategy, acceptance criteria, risks, and include the full original task prompt verbatim in a clearly labelled section at
  the bottom of the document (as strategies/csm-set/docs/plans/examples/phase1-sample.md does).

  Do not write any production code until this plan is complete and self-consistent.

  ---
  Step 3 — Implement SettradeAdapter (Opus subagent for coding)

  Switch to Opus for the implementation work. Follow LiberatorAdapter as the structural template but do NOT cargo-cult Liberator-specific logic (OTP login, PIN handling,
  cancel-then-replace amend).

  File layout (mirror adapters/liberator/ exactly):

  src/quant_execution_engine/adapters/settrade/
      __init__.py
      adapter.py          # SettradeAdapter(BrokerAdapter) — the main class
      client.py           # SettradeHTTPClient — wraps httpx.AsyncClient, handles OAuth lifecycle
      mapper.py           # NormalizedOrder → Settrade wire payload, Settrade response → order status
      errors.py           # SettradeError hierarchy, typed rejection mapping
      models.py           # Pydantic models for Settrade request/response shapes
      reconciler.py       # ReconcileLoop for Settrade (may share base with Liberator's if one exists)

  Mandatory implementation rules:

  Auth / session:
  - OAuth token stored as SecretStr, never in logs, never in error messages.
  - Token refresh must be async, thread-safe (single-flight — no thundering herd on expiry).
  - SettradeHTTPClient must be a context-managed async singleton (follow Liberator's runtime pattern).

  Adapter methods:
  - place_order: map NormalizedOrder → Settrade wire payload via mapper.py; handle typed rejects (symbol not found, price band, margin); persist to execution.orders before calling
  venue (durable-first rule).
  - cancel_order: idempotent; handle already-filled/cancelled gracefully (not an error).
  - amend_order: native — call Settrade's amend endpoint directly; transition through PENDING_REPLACE; handle venue rejection of amend (partial fill race); NO cancel-then-replace.
  - get_order_status: pull current state from Settrade; map to NormalizedOrder status enum.
  - heartbeat / session keepalive: token refresh ping; expose breaker state to /health.
  - reconcile: diff engine DB state vs. Settrade open-order list; repair drift; respect §B fuzzy-match pattern from Phase 3 or supersede with a cleaner design — document the choice.

  Stage matrix (replicate Liberator pattern, adapted for Settrade):
  - sim: SettradeAdapter never instantiated (router uses SimAdapter).
  - paper: session live (can call get_order_status, reconcile), but place_order / cancel_order / amend_order are intercepted to sim. Log the intercept.
  - micro_live: real routes at PTRM-capped size; kill-switch checked first.
  - live: gated — typed reject with clear message until operator explicitly unlocks (same pattern as Liberator's live gate).

  Router wiring:
  - Register SettradeAdapter in the adapter registry / factory so broker="settrade" routes to it.
  - Update capabilities endpoint to include Settrade rows.
  - Update /health to surface Settrade circuit breaker state.

  Settings (src/quant_execution_engine/config.py or equivalent):
  - Add EXECUTION_ENGINE_SETTRADE_APP_ID: SecretStr
  - Add EXECUTION_ENGINE_SETTRADE_APP_SECRET: SecretStr
  - Add EXECUTION_ENGINE_SETTRADE_APP_CODE: SecretStr
  - Add EXECUTION_ENGINE_SETTRADE_BASE_URL: str (with sensible default)
  - Add EXECUTION_ENGINE_SETTRADE_HEARTBEAT_INTERVAL_SECONDS: int = 60 (adjust per broker research)
  - Add EXECUTION_ENGINE_SETTRADE_CIRCUIT_BREAKER_THRESHOLD: int = 3
  - Add EXECUTION_ENGINE_SETTRADE_RECONCILE_INTERVAL_SECONDS: int = 15
  - All must be optional/defaulted so the engine starts without them (broker simply unavailable).

  .env.example: add all new EXECUTION_ENGINE_SETTRADE_* vars with placeholder values and inline comments.

  docker-compose.yml / overlays: add a docker-compose.settrade.yml overlay (mirroring docker-compose.liberator.yml) for owner-mode Settrade bring-up, if a Settrade upstream service
  exists; otherwise document where credentials are injected.

  Error handling:
  - Every Settrade API error must map to a typed rejection envelope (never a raw 500).
  - httpx timeout and connection errors must trip the circuit breaker.
  - OAuth token errors must not leak credentials in exception messages (SecretStr discipline).

  Logging:
  - Structured log lines at key events: token acquired/refreshed, order placed/acked/rejected, amend sent/confirmed/failed, reconcile diff found/resolved, breaker tripped/reset.
  - Never log app_secret, app_code, token values, account numbers, or order quantities that reveal position.

  ---
  Step 4 — Write Tests (Opus subagent)

  Target: ≥ 90% coverage on adapters/settrade/ and all newly touched router/registry code.

  Required test files:

  tests/adapters/settrade/
      test_client.py        # OAuth flow, token refresh, single-flight, expiry, HTTP errors
      test_mapper.py        # NormalizedOrder → wire payload, every supported order_type + tif; response → status
      test_adapter.py       # place/cancel/amend/status/reconcile — happy paths + error paths
      test_circuit_breaker.py  # threshold, trip, mass-cancel trigger, reset
      test_stage_matrix.py  # paper intercepts, micro_live routes, live rejects
  tests/router/
      test_settrade_routing.py  # broker="settrade" routes to SettradeAdapter; capability gate

  Test conventions (from quant-execution-engine/CLAUDE.md):
  - asyncio_mode = "auto" (pytest-asyncio).
  - Mock httpx.AsyncClient — no real network calls in unit tests.
  - Use pytest.mark.integration for skeleton integration tests (excluded from CI by default).
  - Mirror the source layout under tests/.

  Specific scenarios to cover:

  - Token expiry mid-flight: simulate 401, verify token refresh and retry.
  - Native amend accepted → PENDING_REPLACE → PARTIALLY_FILLED → FILLED.
  - Native amend rejected (partial fill race) → adapter raises typed error, state reverts correctly.
  - Kill-switch fires during amend in-flight.
  - Reconcile loop finds drift: engine says NEW, Settrade says FILLED → engine updates.
  - Circuit breaker: 3 consecutive HTTP timeouts → trips → mass-cancel fires → /health reports broker_circuit_open.
  - paper stage: place_order called → intercepted to sim, returns sim ack, does NOT call Settrade HTTP.
  - live stage (without unlock): typed rejection returned, no Settrade call made.
  - Capability gate: broker=settrade, market=SET, order_type=MARKET, tif=DAY passes; unsupported combination returns typed rejection.

  ---
  Step 5 — Run Quality Gates (before any commit)

  cd quant-execution-engine
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src tests
  uv run pytest --cov=src/quant_execution_engine/adapters/settrade --cov-fail-under=90

  Fix all failures. Use Fable model for error review and diagnosis. Do NOT skip or suppress checks with # type: ignore or # noqa unless you document exactly why inline.

  Known baseline: the upstream liberator-trading-api submodule tests have a pre-existing 240F/559P baseline — compare your run against that baseline, not zero.

  ---
  Step 6 — Update Knowledge, Memory, Playbooks, and CLAUDE.md Files

  After implementation passes quality gates, update all of the following. Use Opus for writing.

  quant-execution-engine/CLAUDE.md:

  - Update the "Current state" paragraph to reflect Phase 4 complete.
  - Update the Safety ladder section with Settrade stage matrix details.
  - Add EXECUTION_ENGINE_SETTRADE_* env vars to the Commands / env var reference.

  - Fill in all Phase 4 Settrade rows (the cells previously marked "(confirm P4)").
  - Mark unsupported combinations explicitly.

  - Fill in all Phase 4 Settrade rows (the cells previously marked "(confirm P4)").
  - Mark unsupported combinations explicitly.

  quant-execution-engine/.claude/knowledge/decision-log.md:

  - Add entry for Phase 4: native amend choice, OAuth lifecycle decision, any Settrade-specific trade-offs made during implementation.

  quant-execution-engine/.claude/knowledge/broker-research-settrade.md:

  - Add a section documenting what was actually implemented vs. what the research described (any gaps, deferred items, surprises found during implementation).

  quant-execution-engine/.claude/playbooks/order-routing-safety.md:

  - Add a "Settrade specifics" section (mirror the "Liberator specifics" section):
    - Stage-flip runbook for Settrade (paper → micro_live).
    - OTP / credential rotation procedure if applicable.
    - Breaker reset procedure.
    - Native amend: when to prefer amend vs. cancel-then-replace manually.

  quant-execution-engine/docs/plans/ROADMAP.md:

  - Mark Phase 4 complete with the merge SHA and date.
  - Update Phase 5 "unblocked by" note if applicable.

  Umbrella CLAUDE.md (at repo root):

  - In the feature-execution-engine section, update the Phase 4 status line to "complete (YYYY-MM-DD)" with a one-line description of what landed.

  Umbrella .claude/knowledge/feature-execution-engine.md (the ADR):

  - Update the capability matrix or phase status table if it tracks Phase 4 cells.

  Auto-memory (/home/batt/.claude/projects/-home-batt-docker-quant-trading-system/memory/):

  - Update project-execution-engine-bootstrap.md to reflect Phase 4 complete: adapter name, key implementation facts (native amend, OAuth flow, stage matrix behavior), and "Next:
  Phase 5 = …".
  - If any new non-obvious gotchas emerged (e.g., Settrade amend rejection on partial fill requires special handling), add a feedback-settrade-adapter-gotchas.md memory entry.

  ---
  Step 7 — Commit and Open a Pull Request (Opus)

  Commit sequence (Conventional Commits, tight scope):

  Prefer multiple focused commits over one giant commit:

  1. feat(adapters): add SettradeHTTPClient with OAuth lifecycle
  2. feat(adapters): SettradeAdapter — place/cancel/amend/status/reconcile
  3. feat(adapters): wire SettradeAdapter into router registry + capabilities
  4. test(adapters): SettradeAdapter unit + stage-matrix tests (≥90% cov)
  5. docs(phase4): SettradeAdapter plan + ROADMAP phase 4 complete
  6. chore(config): SETTRADE env vars in config + .env.example
  7. Any knowledge/playbook/CLAUDE.md updates as: docs(knowledge): update capability matrix + decision log for Phase 4

  Each commit message must end with:
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

  PR:

  git push -u origin feature/phase4-settrade-adapter
  gh pr create \
    --title "feat(execution-engine): Phase 4 — SettradeAdapter (native amend, OAuth, second broker)" \
    --body "$(cat <<'EOF'
  ## Summary

  - Implements `SettradeAdapter` — the second real broker adapter, proving the `BrokerAdapter` abstraction scales across venues.
  - **Native order amendment** via Settrade's amend endpoint (`PENDING_REPLACE` state transition) — no cancel-then-replace.
  - OAuth token lifecycle (`app_id`/`app_secret`/`app_code`): async single-flight refresh, `SecretStr` discipline throughout.
  - Stage matrix: `paper` intercepts placements to sim (session live for reads), `micro_live` routes real at PTRM cap, `live` remains gated.
  - Circuit breaker + reconcile loop (adapted §B fuzzy match or new design — see plan for decision).
  - ≥90% test coverage on `adapters/settrade/` + router wiring.
  - Capability matrix Phase 4 cells filled; decision log, safety playbook, and CLAUDE.md updated.

  ## Acceptance criteria

  - [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest` all pass
  - [ ] `broker="settrade"` routes correctly through the adapter registry
  - [ ] Native amend (`PENDING_REPLACE`) state transitions tested and passing
  - [ ] `paper` stage intercepts placement — no real Settrade call made
  - [ ] `live` stage rejects until explicitly unlocked — no real Settrade call made
  - [ ] Circuit breaker trips on 3 consecutive HTTP failures, mass-cancel fires
  - [ ] All new `EXECUTION_ENGINE_SETTRADE_*` env vars documented in `.env.example`
  - [ ] ROADMAP.md marks Phase 4 complete

  ## Test plan

  - Run full suite: `uv run pytest --cov=src/quant_execution_engine/adapters/settrade --cov-fail-under=90`
  - Manual smoke (owner mode, paper stage): bring up with `docker-compose.settrade.yml` overlay, POST a `broker=settrade` order, verify sim ack returned and no Settrade HTTP call
  made.

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"

  ---
  Acceptance Bar

  The implementation is complete when ALL of the following are true:

  1. uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest exit 0.
  2. Test coverage on src/quant_execution_engine/adapters/settrade/ ≥ 90%.
  3. POST /orders with broker="settrade" in paper stage returns a sim ack and does NOT call any Settrade HTTP endpoint.
  4. POST /orders with broker="settrade" in live stage (ungated) returns a typed rejection.
  5. The capability matrix in quant-execution-engine/.claude/knowledge/capability-matrix.md has Phase 4 cells filled with concrete support/not-supported entries for Settrade.
  6. quant-execution-engine/docs/plans/phase4-settrade-adapter.md exists and matches the phase1-sample.md format, including the verbatim task prompt.
  7. quant-execution-engine/docs/plans/ROADMAP.md marks Phase 4 complete.
  8. quant-execution-engine/CLAUDE.md reflects Phase 4 in the current-state block.
  9. The umbrella CLAUDE.md and .claude/knowledge/feature-execution-engine.md reflect Phase 4 complete.
  10. PR is open on GitHub with the box-drawing result table reported back.

  After the PR is created, report the result as an ASCII box-drawing table:

  ┌──────────────────────────────────────────────┬────────────────────────────────────────┬─────────┬──────────────────────────────────────────────────────────────────────────┐
  │                     Repo                     │                Branch                  │ Commit  │                                  GitHub                                  │
  ├──────────────────────────────────────────────┼────────────────────────────────────────┼─────────┼──────────────────────────────────────────────────────────────────────────┤
  │ lumduan/quant-execution-engine (code)        │ feature/phase4-settrade-adapter        │ <sha>   │ PR #N → https://github.com/lumduan/quant-execution-engine/pull/N        │
  └──────────────────────────────────────────────┴────────────────────────────────────────┴─────────┴──────────────────────────────────────────────────────────────────────────┘
````

## Scope

### In Scope

| # | Deliverable | Status |
|---|---|---|
| 1 | `EXECUTION_ENGINE_SETTRADE_*` settings (creds `SecretStr`; base_url, intervals, refresh margin; all optional) + `cryptography>=42` dep + `.env.example` block | Done |
| 2 | `adapters/settrade/errors.py` — `SettradeAdapterError`/`SettradeTransportError`/`SettradeAuthError`/`SettradeMappingError`/`SettradeVenueRejection` | Done |
| 3 | `adapters/settrade/client.py` — OAuth transport: ECDSA sign, single-flight `ensure_token()`, proactive refresh, 401 re-auth, rate-header parse, redaction | Done |
| 4 | `adapters/settrade/models.py` — token/error/place/order-item/portfolio Pydantic models (tolerant, both books) | Done |
| 5 | `adapters/settrade/mapping.py` — pure SET + TFEX payload builders, `wire_price`, read-side venue→normalized, `classify_venue_state` | Done |
| 6 | `adapters/settrade/adapter.py` — `SettradeAdapter(BrokerAdapter)`: place/cancel/**native amend**/reads/heartbeat/capabilities | Done |
| 7 | `adapters/settrade/heartbeat.py` + `reconciler.py` (+ `replace_resolve`) + `runtime.py` (process singleton + worker lifecycle) | Done |
| 8 | `contracts/errors.py` `AmendRejected` (409) + `api/error_handlers.py` status map | Done |
| 9 | `db/repositories.py` `replace_order` (one-statement `PENDING_REPLACE→NEW`) + `fetch_orders_for_reconcile(include_pending_replace=...)` flag | Done |
| 10 | `core/router.py` `amend()` native branch + `settrade_adapter` threaded through all `resolve_adapter` sites; `core/stage.py` settrade axis | Done |
| 11 | `api/{deps,main,routes,schemas}.py` — `PATCH /orders/{cid}` route, `AmendOrderRequest`, settrade runtime injection + lifespan + `/health` brokers block | Done |
| 12 | `contracts/capabilities.py` — SETTRADE×SET (new row) + SETTRADE×TFEX cells pinned from venue docs, `amend="native"`, `adapter_installed=True` | Done |
| 13 | Tests ≥90 % (respx) — `tests/unit/adapters/settrade/` 9-file suite + `test_core_router_amend_native.py` + route/stage/breaker extensions + UAT integration skeleton | Done |
| 14 | Docs: ROADMAP, capability-matrix, decision-log (E21–E27), broker-research-settrade addendum, safety playbook "Settrade specifics", engine CLAUDE.md, umbrella docs | Done |

### Out of Scope (deferred)

- **`MarketRep*`** SDK surface (broker-employee credential class — not the investor plane).
- **`MarketData`** SDK surface (the market-data plane — D1 plane split; stays in `quant-marketdata-engine`).
- **`RealtimeDataConnection` / MQTT** order-update push — Phase 5 (the engine's normalized stream-out).
- **`_place_orders`** private SDK batch method.
- The **`settrade-v2` SDK itself** — we re-implement the wire with raw `httpx.AsyncClient` (Design Decision 2).
- **NVDR** (`trusteeIdType="NVDR"`) — pinned `"Local"` v1; `NormalizedOrder` has no trustee field.
- **`Date` (GTD) TIF**, **`Auto` position effect**, **`SESSION`-trigger stops** — undeclared in v1 (no contract member; see Design Decision 3).
- **`live` stage unlock** — `micro_live` is the highest rung Phase 4 exercises.
- **No compose overlay** — Settrade is a cloud API (Design Decision 9); creds ride `docker-compose.private.yml`'s `env_file`.

## Design Decisions

### 1. Scope amended by operator decision — full SET + TFEX, plus the PATCH amend route

The original task prompt and the old ROADMAP carried a "no SET market on Settrade (derivatives
first)" non-goal. The **operator struck it**: Phase 4 ships **100 % of the `settrade-v2`
investor order surface** — both SET equity (`/api/seos/v3`) and TFEX derivatives
(`/api/seosd/v3`). The operator also added the **`PATCH /orders/{client_order_id}`** HTTP amend
route to scope (the Phase 3 `router.amend` docstring already reads "the route lands Phase 4").
Explicit out-of-scope (declared, not pretended): `MarketRep*`, `MarketData` (D1 plane split),
`RealtimeDataConnection`/MQTT (Phase 5), `_place_orders`.

### 2. Raw `httpx.AsyncClient`, NOT the `settrade-v2` SDK

The official SDK is **sync `requests`** (forbidden in `src/` by hard rule — it blocks the event
loop) and carries **import-time side effects** that are unacceptable in a service: it writes
`~/settradesdkv2_config.txt`, makes an **NTP call**, and fires a **version-check HTTP request**
on import. We re-implement the wire directly. Wire shapes are **pinned** from the SDK v2.2.1
source (extracted at `/tmp/settrade_sdk/settrade_v2/`) cross-checked against the official venue
docs (see "Pinned venue enum sets" below). One new dependency: `cryptography>=42` for ECDSA
P-256 login signing.

*Rejected:* wrapping the SDK in a thread pool (the import side effects and config-file writes
remain; brittle); vendoring the SDK (its sync internals and NTP/version-check stay).

### 3. Native amend rides the frozen `PENDING_REPLACE → NEW` edge — verified against the DB trigger

The frozen 13-edge state machine reserves `PENDING_REPLACE` for native amends with **exactly
one exit (`→ NEW`)**. Verified against the live trigger
(`quant-infra-db/init-scripts/12_schema_execution.sql:203–247`): `'PENDING_REPLACE->NEW'` is a
legal edge; the `price`/`quantity` columns are **unconstrained by the transition guard**; and
the append-only audit trigger **snapshots price/qty on every status change**. Therefore the
amend commit must set `status='NEW'` **and** `price` **and** `quantity` in **one UPDATE**
(`replace_order`, mirroring `ack_order`) so the audit row captures the amended values
atomically. Column CHECKs pre-validated client-side: `quantity > 0`, `display_qty <= quantity`,
`price > 0`.

**Venue amend-reject is a NON-terminal restore.** A partial-fill race (or any venue rejection)
restores `PENDING_REPLACE → NEW`, then `NEW → PARTIALLY_FILLED` (the two-step `cancel_two_step`
precedent) when `filled_qty > 0`, and raises typed `AmendRejected` (HTTP 409). The order is
**still live** — the `reject_reason` column is deliberately **not** written; the evidence is two
audit rows + the typed envelope + a WARNING. There is **no `* → REJECTED` edge from a live
amend** (no ADR amendment needed). If the order meanwhile FILLED at the venue, the reconciler
watermark closes it on the next pass.

**Kill-switch gates amend up front** — amends can *increase* exposure, so the kill-switch check
precedes everything in the amend path (a documented asymmetry vs the un-gated cancel path).
Mass-cancel skips `PENDING_REPLACE` rows **by construction** (`fetch_open_orders` selects only
NEW/PARTIALLY_FILLED; `router.cancel()` raises `IllegalTransition` on PENDING_REPLACE).

*Rejected:* a two-statement update-status-then-update-price (the audit row would snapshot a
stale price); writing `reject_reason` on amend-reject (it would imply the order is dead — it
isn't); a new `PENDING_REPLACE → REJECTED` edge (the frozen machine forbids it and the live
order has no business going terminal).

### 4. `PATCH /orders/{client_order_id}` route — branch on the capability's amend semantics

The route is owner-mode + API-key. It branches on `capabilities.lookup(row.broker, row.market).amend`:

- **`native`** (Settrade): amend in place; the response carries the **same** `client_order_id`.
  `new_client_order_id` must be **absent**.
- **`cancel_replace`** (Liberator): requires `new_client_order_id`; the response carries the
  **replacement** cid (the honest answer — a new order object was created).

This is the single route both broker classes share; the difference is the branch, not the
endpoint. The Phase-3 router-level cancel+replace orchestration is unchanged.

### 5. OAuth token lifecycle — single-flight, proactive refresh, fail-to-login, one reactive retry

`ensure_token()` runs under one `asyncio.Lock` (single-flight — N concurrent callers ⇒ exactly
one login). It refreshes **proactively** inside the 100 s margin (matching the SDK's threshold).
Crucially, **we do NOT copy the SDK's bug** where a refresh failure is silently ignored — on
refresh failure we **fall back to a fresh login**. A reactive **401** triggers **exactly one**
re-auth+retry, guarded by a **token serial** so a burst of 401s doesn't stampede re-auth. The
token, refresh token, `app_secret`, signature, and PIN are all `SecretStr`/redacted — never
logged, never in an exception message.

*Rejected:* refresh-only (a revoked refresh token would dead-end the session); per-request token
acquisition (defeats refresh, hammers the OAM endpoint); unbounded reactive retries (could mask
a persistent auth failure that should trip the breaker).

### 6. Heartbeat = OAuth token-liveness probe (no venue health endpoint exists)

Settrade exposes **no** health/session endpoint. The heartbeat is therefore a **token-liveness
probe**: healthy ⇔ `ensure_token()` succeeds **AND** `last_wire_ok is not False`. Token
*acquirability* IS the OAuth session — if login/refresh works and the last real call didn't
fail, the session is up. This is justified against decision E19 (Liberator probes a real health
route because it has one; Settrade can't). The **residual blind spot** — a token that is valid
but whose order endpoints are degraded — is documented and is fixed in Phase 5 (MQTT gives a
real session signal). N consecutive heartbeat failures (default 3) trip the breaker →
`broker_circuit_open` on new submits + mass-cancel attempted; a healthy probe resets it; the
state surfaces in `/health` and `/capabilities`.

### 7. Reconciler mirrors Liberator (mirror-not-abstract); watermark-delta fills

The reconciler is a **verbatim structural mirror** of `adapters/liberator/reconciler.py` — **no
shared base class yet** (extraction to a common reconciler is deferred to Phase 6; recorded in
the decision log). Fills are **cumulative-watermark deltas** (E18): `delta = matched − db_filled_qty`,
applied once with synthetic `broker_fill_id = f"{order_no}:{matched}"` — re-polls regenerate the
same id and dedupe via `ON CONFLICT DO NOTHING`. We do **not** use `get_trades` (one polling
source, proven idempotency; per-fill granularity is Phase 5 MQTT). The ADR §B constants are
**verbatim**: 5 s stuck threshold, 60 s `ack_lost_unmatched` bound (never re-sent), ±5 s fuzzy
window, **unique-candidate-only** match `(account, symbol, side, qty)`. Polling is grouped per
`(account, market)`. **New action: `replace_resolve`** for a stranded `PENDING_REPLACE` (crash
or lost response mid-amend): venue item resting → one-statement `replace_order` with venue-truth
price/qty; venue terminal → resolve then legal close-out; venue item missing → restore local
values.

### 8. Rate limits — observe-don't-throttle v1

The client parses `X-RateLimit-Remaining-{second,minute}` / `X-RateLimit-Limit-{second,minute}`
into a `rate_snapshot()`; the reconciler consults it and **budget-skips** remaining
`(account, market)` groups when the GET bucket is exhausted; a zero-remaining read logs a
WARNING. We do **not** actively throttle the submit path in v1 (adaptive throttling is Phase 6).
GET and POST/PATCH live in **separate buckets** (SDK defaults 5/s, 60/min), so reconcile reads
never starve order writes.

### 9. No compose overlay — Settrade is a cloud API

Unlike Liberator (which bundles `liberator-trading-api` as a composed upstream), Settrade is a
**cloud Open API** — there is no local service to bring up. Credentials ride
`docker-compose.private.yml`'s `env_file: .env`; there is **no `docker-compose.settrade.yml`**.
The plan documents this in lieu of the prompt's "add an overlay if an upstream exists" branch.

### 10. File layout mirrors the **actual** `adapters/liberator/` names

The prompt sketched `mapper.py` and a client-only transport. We follow the **real** Liberator
package names: `client.py` (OAuth transport), `models.py`, `mapping.py`, `adapter.py`,
`heartbeat.py`, `reconciler.py`, `errors.py`, `runtime.py`. The prompt's `mapper.py` sketch is
superseded by `mapping.py`.

### 11. Stage matrix — symmetric with Liberator, `AdapterIntent` axis reused

`sim` → sim always (broker field ignored). `paper` → TRADE intercepted to sim, READ routes to
Settrade when configured (account/position realism). `micro_live` → `broker=settrade` routes
real at PTRM-capped size. `live` → still `StageRejected` (message updated to "Phase 4"). A
mid-lifecycle stage flip is an operator error covered by the safety playbook (kill-switch +
mass-cancel before flipping). Runtime + heartbeat start iff `stage ∈ {paper, micro_live, live}`
∧ not public mode ∧ **all** of `app_id`/`app_secret`/`app_code`/`broker_id`/`pin` present
(partial ⇒ WARNING + Settrade routing disabled); the reconciler starts only at
`micro_live`/`live` (E15) with `include_pending_replace=True`.

### 12. Settings defaults deviate from the prompt (30/12, not 60/15)

The prompt suggested `HEARTBEAT=60`, `RECONCILE=15`; the prompt itself sanctioned "adjust per
broker research". We mirror Liberator's **30 s heartbeat / 12 s reconcile** cadence for operator
consistency across brokers, and add `TOKEN_REFRESH_MARGIN_SECONDS=100` (the SDK's refresh
threshold). `account_no` is an **integration-test convenience only** — not required to enable
the broker (per-order account comes from `NormalizedOrder.account`).

### Pinned venue enum sets (final — replaces every `(confirm P4)` cell)

Verified this session from the official venue docs
(`developer.settrade.com/.../investor-{derivatives,equity}/*.md`, the raw markdown backend) plus
the `settrade-v2` 2.2.1 SDK source. These values **replace every `(confirm P4)` capability cell**.

**SETTRADE × SET** (`/api/seos/v3`):
- order_types: `LIMIT('Limit')`, `MARKET('MP-MKT')`, `MTL('MP-MTL')`, `ATO('ATO')`,
  `ATC('ATC')`, `ICEBERG('Limit' + qtyOpen=display_qty)`. **STOP/STOP_LIMIT unsupported**
  (no stop API on equity).
- tifs: `DAY('Day')`, `IOC('IOC')`, `FOK('FOK')`, `GTC('Cancel')`. **`Date`(GTD) undeclared**
  (no `Tif` enum member).
- position_effects: `()` (must be `None`).
- amend: **`native`**.

**SETTRADE × TFEX** (`/api/seosd/v3`):
- order_types: `LIMIT('Limit')`, `MARKET('MP-MKT')`, `MTL('MP-MTL')`, `ATO('ATO')`,
  `STOP('MP-MKT' + stop fields)`, `STOP_LIMIT('Limit' + price + stop fields)`,
  `ICEBERG('Limit' + icebergVol)`. **ATC unsupported** (not a derivatives priceType).
- tifs: `DAY`, `IOC`, `FOK`, `GTC('Cancel')`. **`Date` undeclared**.
- position_effects: `(OPEN('Open'), CLOSE('Close'))`. **`Auto` undeclared** (extra permission).
- amend: **`native`**.

**Stop-condition v1 pin** (`NormalizedOrder` has `stop_price` but no condition field):
`BUY → LAST_PAID_OR_HIGHER`, `SELL → LAST_PAID_OR_LOWER`, `stopSymbol = order.symbol`.
`SESSION`-trigger stops undeclared in v1 (recorded as a decision-log entry).

**price=0 rule:** `ATO`/`ATC`/`MP-MTL`/`MP-MKT` (and the `STOP` market leg) must send `price: 0`
on the wire.

**GTC mapping:** `tif=GTC → validityType='Cancel'` (max 254 days; the venue's GTC spelling).

**Status codes** are not exhaustively documented (seen: deriv `E`=Expired, equity `CS`=cancel
confirmed). `classify_venue_state` parses `status` + show-status words conservatively (unknown →
RESTING), trusts `rejectCode != 0` / `rejectReason` first, and corroborates RESTING with
`canCancel`.

## Architecture & Request Flow

### Native amend (`PATCH /orders/{cid}`, micro_live, broker=settrade)

```
PATCH /orders/{client_order_id}  {new_price?, new_qty?}   (owner mode + API key)
  └─ OrderRouter.amend(cid, new_price=, new_qty=)
       branch on capabilities.lookup(row.broker, row.market).amend == "native"
       1 kill-switch FIRST            (amends can increase exposure — gated up front)
       2 fetch row; require new_price or new_qty   (else AmendRejected 409)
       3 precondition row.status ∈ {NEW, PARTIALLY_FILLED}     (else IllegalTransition)
       4 pre-check new_qty > filled_qty ∧ new_qty >= display_qty (else AmendRejected)
       5 PTRM re-check of the hypothetical amended order (NO exemption; risk-reject is safe)
       6 resolve_adapter(intent=TRADE) + breaker.guard()       (broker_circuit_open if OPEN)
       7 update_status(cid, PENDING_REPLACE)     [legal edge, audited]
       8 ack = adapter.amend(...)                [never raises]
            └─ mapping.to_change_payload(...)  → client.patch("orders/{no}/change")
                 ├─ empty-body 2xx          ⇒ AmendAck(ok=True,  semantics="native")
                 └─ {code,message} / 4xx    ⇒ AmendAck(ok=False, semantics="native")
       9a ok   → replace_order(cid, new_price, new_qty)  ONE UPDATE → NEW + price + qty
                  if filled_qty>0: update_status(cid, PARTIALLY_FILLED)   [two-step restore]
                  → 200 NormalizedOrderResult (SAME cid)
       9b fail → update_status(cid, NEW)  (+ PARTIALLY_FILLED restore)
                  → raise AmendRejected(ack.reason) → 409   [order still LIVE; no reject_reason write]
```

### OAuth token lifecycle (in `client.py`)

```
ensure_token():                       [single-flight under one asyncio.Lock]
  token is None / expired             → login()
  expired_at − now <= 100s (margin)   → refresh_token()
        refresh fails                 → login()        [we do NOT copy the SDK silent-ignore bug]
  else                                → reuse cached token (serial unchanged)

login():   POST /api/oam/v1/{broker_id}/broker-apps/{app_code}/login
           {apiKey, params:"", signature=hex(ECDSA-SHA256(f"{app_id}.{params}.{ts}")), timestamp(ms)}
           → {token_type, access_token, refresh_token, expires_in}; serial += 1
refresh(): POST .../refresh-token {apiKey, refreshToken} → same shape; serial += 1
header:    Authorization: {token_type} {access_token}

reactive 401 on any call → if serial unchanged since the request started:
           ensure_token(force=True); retry ONCE; second 401 ⇒ SettradeAuthError (breaker food)
```

### Heartbeat / breaker (every 30 s when stage ≥ paper, owner mode, creds present)

```
heartbeat_loop: ok = adapter.heartbeat()    [ensure_token() succeeds ∧ last_wire_ok is not False]
  ok  → breaker.record_success()            [OPEN → CLOSED reset]
  err → breaker.record_failure()            [N consecutive ⇒ OPEN]
  CLOSED→OPEN transition → on_trip(): OrderRouter.mass_cancel()   (attempted, best-effort)
  /health + /capabilities expose brokers.settrade.{breaker_state, session_healthy}
```

### Reconcile pass (every 12 s at micro_live/live, include_pending_replace=True)

```
fetch_orders_for_reconcile(broker=settrade, include_pending_replace=True)
  group by (account, market) ── rate_snapshot(): GET budget exhausted ⇒ skip remaining groups
    adapter.fetch_venue_orders(account, market) ── index by order_no
  per local row → plan_actions(row, filled_qty, venue_item, now):
    PENDING_NEW + match            → ack_order(cid, order_no)                 [PENDING_NEW→NEW]
    PENDING_NEW >5s, no broker id  → fuzzy (account,symbol,side,qty) ±5s entryTime  (unique only)
    PENDING_NEW >60s unmatched     → REJECTED "ack_lost_unmatched"           [bounded, never re-sent]
    matched > filled_qty           → apply_fill(delta, id=f"{order_no}:{matched}") [NEW→PF→FILLED]
    PENDING_REPLACE stranded       → replace_resolve:                        [NEW action]
         venue resting   → replace_order(venue price/qty)  → NEW (+PF)
         venue terminal  → replace_order then legal close-out
         venue missing   → restore local price/qty → NEW (+PF)
    venue cancelled                → PENDING_CANCEL → CANCELLED              [two-step]
    venue expired                  → EXPIRED
    rejectCode (pre-ack)           → REJECTED + reject_reason
    rejectCode (post-ack)          → set_reject_reason + PENDING_CANCEL→CANCELLED + WARNING
```

## Implementation Steps

1. [x] Branch `feature/phase4-settrade-adapter` (already created); this plan document — standalone `docs(plans)` commit.
2. [x] `chore(config)`: `cryptography>=42` dep; `EXECUTION_ENGINE_SETTRADE_*` settings (creds `SecretStr`, all optional); `.env.example` block (BASE_URL, intervals, refresh margin, UAT-sandbox comment `BROKER_ID=098`).
3. [x] `feat(adapters)`: `errors.py` + `models.py` + `client.py` (ECDSA sign, single-flight `ensure_token`, 401 retry, rate-header parse, redaction) + unit tests (`test_client_auth.py`, `test_client_transport.py`, `test_models.py`).
4. [x] `feat(adapters)`: `mapping.py` (SET + TFEX payloads, `wire_price`, read-side, `classify_venue_state`) + `contracts/capabilities.py` rows (SETTRADE×SET new row + SETTRADE×TFEX cells, `amend="native"`, `installed=True`) + mapping tests (`test_mapping_set.py`, `test_mapping_tfex.py`).
5. [x] `feat(core)`: `contracts/errors.py` `AmendRejected`; `api/error_handlers.py` 409; `db/repositories.py` `replace_order` + `include_pending_replace` flag; `core/router.py` native amend branch + `settrade_adapter` threading; `tests/conftest.py` reset; `test_core_router_amend_native.py`.
6. [x] `feat(adapters)`: `adapter.py` (place/cancel/native amend/reads/heartbeat/capabilities) + `heartbeat.py` + `reconciler.py` (+`replace_resolve`) + `runtime.py` + unit tests (`test_adapter_place.py`, `test_adapter_amend_cancel_reads.py`, `test_heartbeat.py`, `test_reconciler.py`, `test_runtime.py`).
7. [x] `feat(api)`: `core/stage.py` settrade axis; `api/{deps,main}.py` runtime injection + lifespan; `api/schemas.py` `AmendOrderRequest`; `api/routes.py` `PATCH /orders/{cid}` + `/health` brokers block + capability rows + stage/route/breaker test extensions.
8. [x] Enum pinning sweep: confirm capability cells + mapping enums against the pinned venue docs; UAT-sandbox integration skeleton `test_live_settrade_uat.py`.
9. [x] Quality gate green (ruff + format + mypy strict + pytest ≥90 %; liberator submodule baseline 240F/559P — compare against that).
10. [x] `docs:` ROADMAP / capability-matrix / decision-log (E21–E27) / broker-research-settrade addendum / safety playbook / engine CLAUDE.md + Completion Notes here; umbrella docs (separate branch/PR after merge); auto-memory.
11. [ ] Commit sequence → push → PR `feature/phase4-settrade-adapter` → result table. Umbrella pin bump after merge.

## File Changes

| File | Action | Description |
|---|---|---|
| `docs/plans/phase4-settrade-adapter.md` | add | This plan |
| `pyproject.toml` | modify | `cryptography>=42` dependency |
| `src/quant_execution_engine/config/settings.py` | modify | `settrade_*` fields (creds/pin `SecretStr`; base_url, intervals, refresh margin; all optional) |
| `.env.example` | modify | `EXECUTION_ENGINE_SETTRADE_*` block (creds, base_url, intervals, UAT-sandbox `098` comment) |
| `src/quant_execution_engine/adapters/settrade/__init__.py` | add | package export |
| `src/quant_execution_engine/adapters/settrade/errors.py` | add | `SettradeAdapterError`/`SettradeTransportError`/`SettradeAuthError`/`SettradeMappingError`/`SettradeVenueRejection` |
| `src/quant_execution_engine/adapters/settrade/client.py` | add | OAuth transport: ECDSA sign, single-flight `ensure_token`, 401 retry, rate-header parse, redaction |
| `src/quant_execution_engine/adapters/settrade/models.py` | add | token/error/place/order-item/portfolio Pydantic models (tolerant, both books) |
| `src/quant_execution_engine/adapters/settrade/mapping.py` | add | pure SET + TFEX payloads, `wire_price`, read-side venue→normalized, `classify_venue_state` |
| `src/quant_execution_engine/adapters/settrade/adapter.py` | add | `SettradeAdapter(BrokerAdapter)` — place/cancel/native amend/reads/heartbeat/capabilities |
| `src/quant_execution_engine/adapters/settrade/heartbeat.py` | add | heartbeat pass + loop + trip hook (mirror liberator) |
| `src/quant_execution_engine/adapters/settrade/reconciler.py` | add | `plan_actions` (pure, +`replace_resolve`) + `SettradeReconciler` |
| `src/quant_execution_engine/adapters/settrade/runtime.py` | add | process singleton + worker lifecycle |
| `src/quant_execution_engine/contracts/errors.py` | modify | `AmendRejected` (code `amend_rejected`) |
| `src/quant_execution_engine/contracts/capabilities.py` | modify | SETTRADE×SET (new row) + SETTRADE×TFEX cells, `amend="native"`, `adapter_installed=True` |
| `src/quant_execution_engine/api/error_handlers.py` | modify | `AmendRejected` → 409 |
| `src/quant_execution_engine/db/repositories.py` | modify | `replace_order` (one-statement `PENDING_REPLACE→NEW`) + `fetch_orders_for_reconcile(include_pending_replace=...)` |
| `src/quant_execution_engine/core/router.py` | modify | `settrade_adapter` param; `amend()` native branch (kill-switch first, PTRM re-check, two-step restore) |
| `src/quant_execution_engine/core/stage.py` | modify | `settrade_adapter` axis; LIVE message → "Phase 4"; docstring matrix |
| `src/quant_execution_engine/api/deps.py` | modify | inject `get_settrade_adapter()` |
| `src/quant_execution_engine/api/main.py` | modify | lifespan: create/start settrade runtime after liberator; close first on shutdown |
| `src/quant_execution_engine/api/schemas.py` | modify | `AmendOrderRequest` (no-float `new_price`, `new_qty` gt 0, optional UUIDv4 `new_client_order_id`, ≥1 validator) |
| `src/quant_execution_engine/api/routes.py` | modify | `PATCH /orders/{cid}` route; `brokers.settrade` on `/health` + `/capabilities`; amend-route docstrings |
| `tests/_fakes.py` | modify | MemStore `replace_order` + `include_pending_replace`; repo-function list |
| `tests/conftest.py` | modify | settrade runtime reset; `make_settrade_order` |
| `tests/unit/adapters/settrade/test_*.py` | add | `test_client_auth` / `test_client_transport` / `test_mapping_set` / `test_mapping_tfex` / `test_models` / `test_adapter_place` / `test_adapter_amend_cancel_reads` / `test_heartbeat` / `test_reconciler` / `test_runtime` |
| `tests/test_core_router_amend_native.py` | add | full native accept flow (exact call sequence) + venue-reject race + preconditions + PTRM no-exemption + kill-switch + cid rules + sim-stage e2e both brokers |
| `tests/test_api_routes.py`, `tests/test_core_stage_matrix.py`, `tests/test_core_router_liberator.py` | modify | PATCH happy/guards/envelopes; paper-intercept respx zero-HTTP; live gated; breaker e2e; capability gate |
| `tests/integration/adapters/settrade/test_live_settrade_uat.py` | add | `@pytest.mark.integration` UAT-sandbox (broker `098`) skeleton |
| `docs/plans/ROADMAP.md`, `.claude/knowledge/{capability-matrix,decision-log,broker-research-settrade}.md`, `.claude/playbooks/order-routing-safety.md`, `CLAUDE.md` | modify | Phase 4 status, pinned cells, E21–E27, Settrade specifics, current-state block |

## Success Criteria

- [x] The **same** `NormalizedOrder` routes to either broker by `broker`/account with no contract change — `broker=settrade` reaches Settrade (mock-verified end-to-end; live operator-gated).
- [x] Native amend rides `PENDING_REPLACE → NEW` as one atomic `replace_order` UPDATE; venue amend-reject restores non-terminally (NEW, +PARTIALLY_FILLED when filled) and raises `AmendRejected` (409) — never REJECTED.
- [x] `PATCH /orders/{cid}` works for both amend classes: native returns the same cid, cancel_replace returns the replacement cid; typed envelopes (404/409/403/422/503) pass through.
- [x] OAuth lifecycle: single-flight (N concurrent ⇒ 1 login), proactive refresh inside 100 s, refresh-fail→login fallback, exactly one reactive 401 retry; no credential ever logged or in an exception.
- [x] Capability matrix has all Phase 4 Settrade cells filled (SET + TFEX) from the pinned venue enums; unsupported combos (SET stops, TFEX ATC, `Date`/`Auto`/NVDR/SESSION) reject pre-flight before any HTTP.
- [x] Reconciler v1 drives PENDING_NEW → NEW → PARTIALLY_FILLED → FILLED from polled venue truth; lost-ack fuzzy match works; `replace_resolve` repairs stranded PENDING_REPLACE; resolution bounded; rate-budget skip honored.
- [x] Heartbeat + circuit breaker: N consecutive failures trip → `broker_circuit_open` on submits + mass-cancel attempted; healthy probe resets; state visible in `/health` and `/capabilities`.
- [x] `paper` intercepts placement to sim with **zero** Settrade HTTP calls; `micro_live` routes real at PTRM cap; `live` stays gated.
- [x] Kill-switch gates amend up front (asserted paired against the un-gated cancel path); PTRM re-checks the hypothetical amended order with no exemption.
- [x] Quality gate green: ruff + ruff format + mypy strict + pytest ≥90 % on `adapters/settrade/` (respx-mocked; no live creds; liberator submodule baseline 240F/559P).

## Completion Notes

### Summary

Landed on `feature/phase4-settrade-adapter` (2026-06-11), gate-green. `adapters/settrade/` is the
**second real broker** — full SET equity + TFEX derivatives behind one `NormalizedOrder`, with
**zero contract change** (no new edge, no infra-db migration; native amend rides the existing
frozen `PENDING_REPLACE → NEW` edge, verified against the live trigger). The wire is a raw
`httpx.AsyncClient` (Design Decision 2 — the sync `settrade-v2` SDK is forbidden): ECDSA P-256
login signing, single-flight `ensure_token()` with proactive refresh + refresh-fail→fresh-login
+ one reactive-401 retry; secrets/tokens/signature/account redacted throughout. Native amend via
the new `PATCH /orders/{client_order_id}` route (kill-switch-gated up front, PTRM no-exemption,
one atomic `replace_order`, non-terminal venue-reject restore + `AmendRejected` 409). Reconciler
v1 mirrors Liberator (watermark fills + new `replace_resolve` for stranded `PENDING_REPLACE`,
observe-don't-throttle rate budget). OAuth token-liveness heartbeat + breaker; stage matrix
(`paper` intercept / `micro_live` real / `live` gated). Capability cells pinned from
`developer.settrade.com`. No compose overlay (cloud API).

**Final gate:** ruff + ruff format + mypy strict all clean; **pytest 687 passed**; **total
coverage 96.14%** — settrade modules: adapter 94%, heartbeat 95%, reconciler 93%, runtime 96%,
client 96%, mapping 97%, models 97%, errors 100%. (Liberator submodule baseline 240F/559P
unchanged — Phase 4 touched no upstream code.)

### Issues encountered

None blocking. The one notable surprise was **positive**: the official venue docs
(`developer.settrade.com`), recorded in the Phase-3 research note as an un-scrapable JS SPA,
turned out to serve their content as **raw markdown** from a `/template/open-api/...` backend
(menu `config.json` + `{n}_{name}.md` pages). That discovery converted the planned enum-pinning
**fallback** (declare conservative cells, validate at micro_live) into **verified cells** pinned
before any code shipped — every former `(confirm P4)` cell is now doc-pinned (recorded as
decision E26 and as a reusable scraping recipe in `broker-research-settrade.md`).

### Decisions changed from plan

- **File-size target ≤ 400 lines exceeded for two files, by design:** `mapping.py` is **460
  lines** (two complete venue books — SET + TFEX — each with place/amend/cancel payload builders,
  `wire_price`, read-side venue→normalized, and `classify_venue_state`; splitting it would scatter
  one cohesive pure-mapping concern) and `core/router.py` is **445 lines** (the native-amend
  orchestration — kill-switch-gate, PTRM re-check, two-step restore, venue-reject handling — added
  to the existing submit/cancel paths). Both stay single-responsibility; the line budget is a
  target, not a hard cap.
- **`adapter.get_budget_exhausted()` seam:** rather than have the reconciler reach into the
  client's private rate-snapshot state, the adapter exposes a `get_budget_exhausted()` boolean
  (decision E25) — keeps the reconciler off `client.py` internals.
- **In-Scope rows flipped Planned → Done** (all 14); the §"Pinned venue enum sets" cells in this
  plan are now realized 1:1 by `contracts/capabilities.py` + `adapters/settrade/mapping.py`.

---

**Document Version:** 1.1
**Status:** Complete (2026-06-11)

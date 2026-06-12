# Phase 5.1: Strategy Execution Flags + Sim Trade Loop

**Feature:** feature-execution-engine — Phase 5.1: Strategy execution flags + sim trade loop
**Branch:** `docs/phase5.1-strategy-execution-flags-sim-trade-loop` (this repo, docs-only) — code lands in the strategy repos
**Created:** 2026-06-12
**Status:** In Progress
**Depends On:** Phase 5 engine side (Complete — engine PR #10, infra-db PR #15, gateway PR #23, all merged)

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

Phase 5.1 is the split-out **strategy-side** scope of Phase 5 (operator decision, 2026-06-12):
both strategy repos become first-class callers of the engine surface that Phase 5 shipped.
Each strategy gains an `*_EXECUTION_MODE` flag (`off | sim | live`, default `off`) and an
end-to-end sim trade loop:

```
signal → NormalizedOrder → POST /orders (gateway proxy) → GET /orders/stream (SSE)
       → sim fill events → local position update
```

Strategies hold **no broker credential and no broker code** — they speak only the frozen
`NormalizedOrder` contract through `/api/v2/engines/execution/*`. The engine is consumed
**as-is**: no engine code changes in this phase (this repo's PR is documentation only).
`live` remains explicitly flagged and gated (typed `stage_rejected`); strategy-side settings
validation additionally rejects `live` whenever the strategy runs in public mode.

One adjacent gap is closed in `quant-api-gateway`: the execution proxy built a fresh header
dict and dropped `X-Strategy-Id`, so the D16 attribution (persisted
`execution.orders.strategy_id`, restart-safe stream filtering) could not work through the
gateway. A one-line forwarding fix per proxy helper (+ tests) ships as its own small PR
(operator-approved deviation from the "no gateway change" non-goal).

### Parent Plan Reference

- [`docs/plans/ROADMAP.md`](ROADMAP.md) — § "Phase 5.1 — Strategy execution flags + sim trade loop"
- [`docs/plans/phase5-strategy-execution-path-order-streaming.md`](phase5-strategy-execution-path-order-streaming.md) — the shipped engine surface this phase consumes
- Umbrella: `plans/feature-execution-engine/ROADMAP.md`

### Key Deliverables

1. **`strategies/csm-set`** — `CSM_EXECUTION_MODE` (+ `CSM_EXECUTION_ACCOUNT`, `CSM_EXECUTION_BROKER`) settings; `src/csm/execution/{models,engine_adapter,sim_loop}.py` (+ `errors.py` extension); `scripts/verify_execution_sim.py`; tests; docs. PR → `live-test`.
2. **`strategies/tfex-s50-multi-tf-swing`** — `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE` (+ account/broker) settings; same module set with TFEX deltas (`position_effect` required, OPEN/CLOSE inference, int contracts); tests; docs. PR → `main`.
3. **`quant-api-gateway`** — forward `X-Strategy-Id` in `_proxy` + `_proxy_sse` (+ tests). PR → `main`.
4. **This repo (docs only)** — this plan doc; `CLAUDE.md` + `docs/plans/ROADMAP.md` Phase 5.1 status; `.claude/knowledge/order-update-stream.md` strategy-consumer-contract addendum. PR → `main`.
5. **Umbrella** — pin bumps (4 repos) + `CLAUDE.md` / knowledge / feature-ROADMAP updates (commit only; push is a separate operator action).

---

## AI Prompt

The following operator prompt is being executed (verbatim):

```
You are implementing **Phase 5.1 — Strategy execution flags + sim trade loop** for the
`quant-trading-system` umbrella. This is the strategy-side counterpart of the already-shipped
Phase 5 engine side. Read the documents listed below BEFORE writing a single line of code.

---

## 0. Read These Documents First (in this order)

1. `CLAUDE.md` — umbrella system map, ownership boundaries, submodule rules, port allocations
2. `quant-execution-engine/CLAUDE.md` — execution engine context: safety ladder, NormalizedOrder
   contract, stage semantics, `X-Strategy-Id`, gateway surface, frozen constraints
3. `quant-execution-engine/docs/plans/ROADMAP.md` — full phase history; Phase 5.1 scope block
   at § "Phase 5.1 — Strategy execution flags + sim trade loop"
4. `quant-execution-engine/docs/plans/phase5-strategy-execution-path-order-streaming.md` —
   the shipped engine surface you are wiring to (SSE schema, event types, stream filtering)
5. `strategies/csm-set/CLAUDE.md` — csm-set architecture, layering rules, settings pattern
6. `strategies/csm-set/src/csm/config/settings.py` — `ohlcv_source` as the rollout-flag precedent
7. `strategies/tfex-s50-multi-tf-swing/CLAUDE.md` — tfex architecture and settings prefix
8. `strategies/tfex-s50-multi-tf-swing/src/tfex_s50_multi_tf_swing/config/settings.py` —
   existing settings fields + tfex-specific types
9. `strategies/csm-set/docs/plans/examples/phase1-sample.md` — plan doc format reference

---

## 1. Model Selection Rules (MANDATORY — follow exactly)

| Activity | Model | Effort |
|---|---|---|
| Thinking, planning, design decisions | `claude-fable-5` | xHigh |
| Coding, writing tests | `claude-opus-4-8` (subagent) | default |
| Fix errors, review code | `claude-fable-5` | default |
| Create/update docs, commit messages, PRs | `claude-opus-4-8` | default |

Spawn Opus subagents for all coding tasks. Never use Fable for routine coding.

---

## 2. Objective

Add a strategy-side execution path — behind an `*_EXECUTION_MODE` flag — to both strategy
repos. When enabled (`sim`), each strategy converts its signals to `NormalizedOrder`s, submits
them via the gateway's execution surface, and consumes `GET /orders/stream` for fill callbacks
that update its sim position. Strategies never hold a broker credential; no engine code changes.

**Acceptance:** a strategy runs the full loop `signal → NormalizedOrder → POST /orders →
GET /orders/stream → sim fill → position update` entirely against the engine's `SimAdapter`,
with no broker code in the strategy; `live` remains explicitly flagged and gated (typed error).

---

## 3. Scope

### 3a. Repos and PRs

| Repo | Work | PR target |
|---|---|---|
| `strategies/csm-set` | Add execution adapter + `CSM_EXECUTION_MODE` | own PR on `csm-set` |
| `strategies/tfex-s50-multi-tf-swing` | Add execution adapter + `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE` | own PR on tfex repo |
| `quant-execution-engine` | Plan doc only (no code change) | own PR on execution-engine repo |
| umbrella | Pin bumps + plan-doc commit | bump commit on `main` |

**No engine code changes.** The full execution surface shipped in Phase 5 is consumed as-is.

### 3b. Deliverables per strategy repo

#### `strategies/csm-set`

1. **`CSM_EXECUTION_MODE`** setting (`off` | `sim` | `live`, default `off`) added to
   `strategies/csm-set/src/csm/config/settings.py`. Follow the `ohlcv_source` field pattern
   exactly: `Field(default="off")`, a `@field_validator` that rejects unknown values with a
   clear error, and a `@model_validator(mode="after")` that rejects `live` unless
   `public_mode=false` (owner mode).
2. **`strategies/csm-set/src/csm/execution/engine_adapter.py`** — new module (alongside the
   existing `simulator.py`, `slippage.py`). Provides:
   - `ExecutionEngineAdapter` — async context manager; wraps `httpx.AsyncClient` targeting the
     gateway (`CSM_GATEWAY_BASE_URL`) with header `X-API-Key` + `X-Strategy-Id: csm-set`.
   - `submit_order(order: NormalizedOrder) -> NormalizedOrderResult` — `POST
     /api/v2/engines/execution/orders`; raises `ExecutionError` on non-2xx.
   - `stream_updates(filter_strategy_id: str = "csm-set") -> AsyncIterator[OrderUpdateEvent]`
     — consumes `GET /api/v2/engines/execution/orders/stream` (SSE); yields parsed
     `OrderUpdateEvent` Pydantic models; handles `Last-Event-ID` reconnect; terminates on
     `resync_required` advisory with a `StreamResetError`.
   - `NormalizedOrder` and `OrderUpdateEvent` are defined locally in
     `strategies/csm-set/src/csm/execution/models.py` (Pydantic v2, `Decimal`-as-str on wire;
     never import across the repo boundary into the engine repo).
3. **`strategies/csm-set/src/csm/execution/sim_loop.py`** — the end-to-end sim trade loop:
   - `run_sim_loop(signals: list[Signal], portfolio: Portfolio, adapter: ExecutionEngineAdapter)`
     — converts each signal to a `NormalizedOrder` (SET equity: `broker="sim"`,
     `market="SET"`, `order_type="LIMIT"`, `tif="DAY"`, SET `position_effect=None`), submits,
     awaits fill events from the stream, and updates the portfolio positions.
   - Must be exercised only when `CSM_EXECUTION_MODE != "off"`.
4. **`strategies/csm-set/src/csm/execution/__init__.py`** — re-exports.
5. **Tests**: `tests/unit/execution/test_engine_adapter.py` + `tests/unit/execution/test_sim_loop.py`
   — mock the httpx client and the SSE stream; cover: happy-path submit + fill event → position
   update, partial fill, rejected order, stream reconnect, `resync_required` error, `live` mode
   rejected when `public_mode=true`.
6. **No live orders**: when `CSM_EXECUTION_MODE=live` reject at settings validation if
   `CSM_PUBLIC_MODE=true`; in the adapter, if mode is `live` but the engine returns a
   `stage_mismatch` typed rejection, surface it as `ExecutionError` with the original message.

#### `strategies/tfex-s50-multi-tf-swing`

Same pattern as csm-set with these differences:
1. **`TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE`** in
   `strategies/tfex-s50-multi-tf-swing/src/tfex_s50_multi_tf_swing/config/settings.py`.
2. **`strategies/tfex-s50-multi-tf-swing/src/tfex_s50_multi_tf_swing/execution/engine_adapter.py`**
   — `X-Strategy-Id: tfex-s50-multi-tf-swing`.
3. **`strategies/tfex-s50-multi-tf-swing/src/tfex_s50_multi_tf_swing/execution/sim_loop.py`**
   — TFEX-specific order construction: `broker="sim"`, `market="TFEX"`,
   `order_type="LIMIT"`, `tif="DAY"`, **`position_effect` required** (`OPEN` for entry,
   `CLOSE` for exit — infer from existing position direction; never send `None` on TFEX).
   S50 contracts are integer quantities; `price` is `Decimal` (two decimal places).
4. **Tests**: same coverage pattern; additionally cover `position_effect` inference (OPEN vs
   CLOSE) and the TFEX `broker_order_id` being an `int` string (from the engine).
5. **`X-Strategy-Id`** must be the string `"tfex-s50-multi-tf-swing"` (matches the gateway
   registration name).

### 3c. Shared design constraints (apply to both repos)

- **No broker code in strategies.** Strategies construct `NormalizedOrder(broker="sim", ...)`.
  The `live` path constructs the same object with `broker` sourced from a setting (not hardcoded);
  the actual routing decision belongs to the engine.
- **Gateway-proxied.** All calls go to `{GATEWAY_BASE_URL}/api/v2/engines/execution/*` —
  never directly to `:8400`.
- **`Decimal`-as-str on wire.** `price`, `stop_price`, `avg_fill_price` — `str(Decimal(...))`
  when serializing, `Decimal(str_val)` when deserializing. No `float` at any money boundary.
- **SSE stream lifetime.** The stream connection is per-session (one connection per strategy
  run, not per order). Use `asyncio.TaskGroup` or `anyio` to drive submit + stream concurrently.
- **`Last-Event-ID` reconnect.** Track the last received `seq` and send it as `Last-Event-ID`
  on reconnect. On `resync_required`, re-fetch order state via `GET /orders/{client_order_id}`
  before resuming.
- **`X-Strategy-Id` on every request.** Both `POST /orders` and `GET /orders/stream`.
- **UUIDv4 `client_order_id`** — generated by the strategy per order, not reused on retry
  (generate a new one; the engine dedupes at-least-once, not exactly-once).
- **`off` mode is the zero-code path.** When `EXECUTION_MODE=off`, the strategy's existing
  internal sim (`simulator.py`, existing `execution/engine.py`) runs unchanged. The new adapter
  is never instantiated.
- **Mypy strict.** Both repos already run `mypy --strict`. All new code must pass with no
  `type: ignore` comments.
- **No new third-party dependencies.** `httpx` is already a dependency in both repos (used by
  the gateway adapter). Do not add `sseclient`, `httpx-sse`, or any SSE library; implement the
  protocol with `httpx`'s `aiter_lines` or raw streaming (matching the pattern used in the
  gateway proxy — `client.stream(...)` + `aiter_raw`). Check existing `pyproject.toml` first.

---

## 4. Plan Document (write BEFORE any code)

Before writing implementation code, use `claude-fable-5` at xHigh effort to think through the
design and produce the plan document at:

quant-execution-engine/docs/plans/phase5.1-strategy-execution-flags-sim-trade-loop.md

Follow the format in `strategies/csm-set/docs/plans/examples/phase1-sample.md` exactly:
frontmatter fields, Table of Contents, Overview, AI Prompt, Scope (In Scope / Out of Scope
tables), Design Decisions, Implementation Steps, File Changes, Success Criteria. Include the
verbatim text of the prompt you are executing in the "AI Prompt" section.

The plan document lives in `quant-execution-engine/docs/plans/` (the execution engine owns
the phase plan series) and should cross-reference the two strategy-repo PRs and the umbrella
pin bump. Commit the plan doc to the execution-engine repo's own branch.

---

## 5. Knowledge and Memory Updates

After the plan is written and the implementation is complete, update the following:

### Must update:
- `quant-execution-engine/CLAUDE.md` — advance Phase 5.1 status from `[ ] Proposed` to
  `[x] Complete`; record test counts and coverage; add a one-line "Phase 6 next" note.
- `CLAUDE.md` (umbrella) — update the `feature-execution-engine` row: Phase 5.1 status,
  strategy repos affected, and the `*_EXECUTION_MODE` flags added.
- `.claude/knowledge/feature-execution-engine.md` (umbrella) — append Phase 5.1 completion
  note with date, PRs, and flags.
- Umbrella auto-memory at `/home/batt/.claude/projects/-home-batt-docker-quant-trading-system/memory/`:
  update `project-execution-engine-bootstrap.md` to reflect Phase 5.1 complete, including the
  `CSM_EXECUTION_MODE` and `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE` flags.

### May need to create/update:
- `strategies/csm-set/CLAUDE.md` — note the new `CSM_EXECUTION_MODE` flag, the execution
  adapter location, and the `off` default.
- `strategies/tfex-s50-multi-tf-swing/CLAUDE.md` — same for the tfex flag.
- `strategies/csm-set/.claude/` and `strategies/tfex-s50-multi-tf-swing/.claude/` — if
  execution-mode knowledge or playbooks are absent, add a short `knowledge/execution-mode.md`
  describing the flag values and the end-to-end sim loop flow.

---

## 6. Git Workflow (MANDATORY — read the submodule rules first)

> The umbrella uses git submodules. `strategies/csm-set` tracks branch `live-test`;
> `strategies/tfex-s50-multi-tf-swing` tracks `main`. **Always** `git -C <path> switch
> <branch>` before committing inside a submodule. The umbrella never commits *into* a
> submodule — it only bumps pins. Commits made on a detached HEAD are stranded.

### Step-by-step:

1. **csm-set repo**: `git -C strategies/csm-set switch live-test` → create
   `feature/phase5.1-execution-flags-sim-loop` branch → implement + tests + docs → commit
   (quality gate must pass) → push → open PR targeting `live-test`.
2. **tfex repo**: `git -C strategies/tfex-s50-multi-tf-swing switch main` → create
   `feature/phase5.1-execution-flags-sim-loop` → implement + tests + docs → commit → push →
   open PR targeting `main`.
3. **execution-engine repo**: `git -C quant-execution-engine switch main` → create
   `docs/phase5.1-strategy-execution-flags-sim-trade-loop` branch → add plan doc + update
   `CLAUDE.md` + update `.claude/knowledge/` → commit → push → open PR targeting `main`.
4. **Umbrella**: after all three PRs merge, `git add strategies/csm-set
   strategies/tfex-s50-multi-tf-swing quant-execution-engine` → commit
   `chore: bump pins — csm-set + tfex + execution-engine (Phase 5.1)` → do NOT push without
   explicit user approval (the umbrella `main` push is a separate action).

Quality gate before every push (per-repo, using that repo's tool chain — all via `uv run`):
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
Coverage target: ≥90% on new execution modules in each strategy repo.

---

## 7. Success Criteria

- [ ] `CSM_EXECUTION_MODE=sim` in csm-set's `.env`: strategy submits a `NormalizedOrder` to the
      engine (via gateway), receives fill events over SSE, and updates its portfolio position.
- [ ] `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE=sim` in tfex: same loop, with correct
      `position_effect=OPEN/CLOSE` and TFEX order types.
- [ ] `EXECUTION_MODE=off` (default): existing internal sim path runs unchanged; no adapter
      instantiated; no API call made.
- [ ] `EXECUTION_MODE=live` with `PUBLIC_MODE=true`: settings validation raises `ValueError`
      before the service starts.
- [ ] All new modules pass `mypy --strict` with zero `type: ignore`.
- [ ] `pytest` coverage ≥90% on new execution modules in each strategy repo.
- [ ] No `float` at any money boundary (all prices/quantities use `Decimal` or `int`).
- [ ] No broker credential in any strategy file.
- [ ] Three PRs open + umbrella pins committed.
- [ ] Plan doc at `quant-execution-engine/docs/plans/phase5.1-strategy-execution-flags-sim-trade-loop.md`
      committed.
- [ ] All knowledge files and CLAUDE.md files updated.

---

## 8. Non-Goals (do not implement)

- Any change to the execution engine, infra-db, or gateway code.
- `live` order routing (flag exists in code; settings validation gates it; no live order
  path is exercised in this phase).
- Per-strategy API keys / JWT auth on the engine (§J — deferred).
- Multi-worker fan-out, Redis pub/sub (§H — deferred).
- TFEX `position_effect=Auto` or `GTC`/`GTD` TIF (not in the Settrade capability matrix).
- Any changes to the existing internal `simulator.py` / `slippage.py` logic.

---

## 9. Commit and PR Table

After all work is done, report results as the ASCII box-drawing table required by the global
CLAUDE.md (`Repo | Branch | Commit | GitHub`).
```

---

## Scope

### In Scope (Phase 5.1)

| Component | Repo | Description |
|---|---|---|
| `CSM_EXECUTION_MODE` + `CSM_EXECUTION_ACCOUNT` + `CSM_EXECUTION_BROKER` | csm-set | Settings flag trio (`off\|sim\|live`, default `off`); whitelist validators + `model_validator` gating (`live`+public_mode rejected; mode≠off requires gateway URL/key/account) |
| `src/csm/execution/models.py` | csm-set | Local Pydantic wire mirrors (SET-only `NormalizedOrder`, `NormalizedOrderResult`, `OrderUpdateEvent`, `FillEvent`) + `OrderInstruction`, `SimPortfolio` |
| `src/csm/execution/engine_adapter.py` | csm-set | `ExecutionEngineAdapter` — gateway-targeted httpx client; `submit_order`, `get_order`, `stream_updates` (hand-rolled SSE, `Last-Event-ID` reconnect, seq watermark) |
| `src/csm/execution/sim_loop.py` | csm-set | `build_order_instructions` (TradeList + prices → priced instructions) + `run_sim_loop` (TaskGroup: subscribe-before-submit, single-source fill accounting, GET-residual reconcile) |
| `scripts/verify_execution_sim.py` | csm-set | Committed operator CLI for the live end-to-end sim verify |
| TFEX counterparts of all of the above | tfex | `TFEX_S50_MULTI_TF_SWING_*` prefix; `market="TFEX"`; `position_effect` **required** with OPEN/CLOSE inference; int contracts; entry+exit verify |
| `X-Strategy-Id` forwarding | quant-api-gateway | `_proxy` + `_proxy_sse` forward the header (one-liner each + tests) — operator-approved 4th PR |
| Tests ≥90% on new modules | both strategies + gateway | Mocked httpx/SSE suites per the test matrix; settings validation cases |
| Docs + knowledge | all four repos + umbrella | This plan doc; CLAUDE.md sections; `.claude/knowledge/execution-mode.md` per strategy; stream consumer-contract addendum; umbrella row/knowledge/memory updates |

### Out of Scope (Phase 5.1)

| Item | Disposition |
|---|---|
| Engine / infra-db code changes | None — surface consumed as-is (this repo's PR is docs-only) |
| `live` order routing | Flag exists; settings + engine stage-gating reject it; no live path exercised |
| Wiring the sim loop into production pipelines (csm daily refresh, tfex runner) | Deferred — library + verify script only (operator decision) |
| Per-strategy API keys / JWT auth | §J — deferred |
| Multi-worker fan-out / Redis pub/sub | §H — deferred |
| TFEX `position_effect=Auto`, `GTC`/`GTD` TIF | Not in the Settrade capability matrix |
| Position flips (opposite-direction order larger than the open position) | `SimLoopError` — close-then-reopen is not supported in 5.1 |
| Changes to existing `simulator.py` / `slippage.py` / tfex `execution/engine.py` | Untouched |

---

## Design Decisions

1. **`client_order_id` retry semantics (deliberate interpretation of the prompt).** The prompt
   says "not reused on retry (generate a new one)". The frozen ADR §A contract, however, is
   **at-least-once submission + engine-side dedupe on `client_order_id` + idempotent
   re-submit** — generating a *fresh* cid on a transport retry would defeat the dedupe and
   risk double execution. Resolution: **fresh `uuid4()` per logical order; the SAME cid is
   reused on transport/bare-5xx retries of one submission** (a 200 "prior ack" parses
   identically to a 201). A re-attempt after a terminal REJECT is a new logical order → new cid.
2. **Single-source position accounting.** Fills are applied **only** from stream `fill`
   events; the POST ack never moves positions. The stream is subscribed **before** the first
   submit, so the synchronous-SimAdapter case (ack already `FILLED`) is covered by the
   replayed stream events — applied exactly once. Fallback on per-order timeout or stream
   reset: `GET /orders/{cid}` and apply the **residual** (`filled_qty − applied_qty`) at
   `avg_fill_price`.
3. **Client-side seq watermark.** `stream_updates` skips any event with
   `seq <= last_seen_seq` — the guard against duplicates across client reconnects (the engine
   already dedupes its own subscribe/replay overlap server-side). Reconnects send
   `Last-Event-ID: <last_seen_seq>`.
4. **Advisory frames.** `resync_required` → the adapter raises `StreamResetError(after_seq)`;
   the sim loop catches it and degrades to GET-polling for in-flight orders (it does not crash
   the run). `gap` → WARNING + continue (the timeout + GET-residual path recovers a lost
   terminal event).
5. **Typed envelopes are terminal; bare transport 5xx retries.** A response carrying the
   engine's `{"error": {code, message, …}}` envelope — including 503 `kill_switch_engaged` —
   raises `OrderRejectedError` with the original code/message and is never retried. Bare
   502/503/504 (gateway transport mapping) and `httpx.HTTPError` retry with backoff, same cid.
6. **Strategy `public_mode` forbids only `live`.** `sim` is allowed in public mode (tfex
   defaults `public_mode=True` in code); the engine's own public-mode/stage gates remain the
   real submission guards.
7. **Local wire mirrors, no cross-repo imports.** Each strategy defines its own Pydantic
   mirrors with `Literal` enums and a `WireDecimal` (`PlainSerializer(format(d, "f"))`)
   matching the engine's. csm's `wire_dump()` uses `exclude_none=True` so `position_effect`
   never appears on the SET wire; the tfex mirror makes `position_effect: Literal["OPEN",
   "CLOSE"]` a **required** field.
8. **Settings trio, gateway settings reused.** `execution_mode` / `execution_account`
   (NormalizedOrder.account is required, min_length 1) / `execution_broker` (default `sim`;
   names the live-path venue, ignored in sim). The existing `gateway_base_url` +
   `gateway_api_key` are reused — no new URL settings.
9. **TFEX `position_effect` inference.** No position or same-direction → `OPEN`;
   opposite-direction with `contracts <= position.contracts` → `CLOSE`; oversize flip →
   `SimLoopError`. `broker_order_id` stays `str` end-to-end (TFEX venue ids are numeric
   strings; never int-parsed).
10. **csm `Trade` has no price.** `build_order_instructions(trade_list, prices)` takes an
    explicit `Mapping[str, Decimal]`; `HOLD` / `delta_shares == 0` are skipped (reported);
    side derives from the sign of `delta_shares`; a traded symbol missing from `prices` is a
    loud `SimLoopError`.
11. **Gateway `X-Strategy-Id` forwarding (operator-approved 4th PR).** Discovered during
    planning: `_proxy` builds a fresh header dict (only `X-API-Key`/`Content-Type`), so D16
    attribution silently broke through the gateway. The stream's `?strategy_id=` filter is a
    query param (already passes through); the POST header is what the fix unblocks —
    persisted `execution.orders.strategy_id` + DB-seeded restart-safe stream filtering.

---

## Implementation Steps

1. **Plan doc first** (this document, Fable xHigh) — committed on this branch before any code.
2. **Gateway PR** (`feature/phase5.1-forward-strategy-id` → `main`) — header forwarding +
   tests; gate; merge early so the live verify runs against a rebuilt gateway.
3. **csm-set PR** (`feature/phase5.1-execution-flags-sim-loop` → `live-test`) — settings →
   errors → models → adapter → sim loop → verify script → tests → CLAUDE.md + knowledge; gate.
4. **tfex PR** (`feature/phase5.1-execution-flags-sim-loop` → `main`) — port of csm with the
   TFEX deltas; gate.
5. **Live end-to-end verify** (before merging the strategy PRs): engine flipped to owner-mode
   `sim` (`docker compose -f docker-compose.yml -f docker-compose.private.yml up -d`;
   `/health` → `stage: sim, public_mode: false`), gateway rebuilt at host `:8080`; run both
   `scripts/verify_execution_sim.py` with `*_EXECUTION_MODE=sim`; cross-check
   `GET /orders/{cid}` = FILLED and `execution.orders.strategy_id` stamped; **restore the
   engine to public mode**. Evidence lands in Completion Notes below.
6. **Merge** strategy PRs, then this docs PR (with Completion Notes + ROADMAP/CLAUDE.md flips).
7. **Umbrella**: CLAUDE.md row + `.claude/knowledge/feature-execution-engine.md` addendum +
   `plans/feature-execution-engine/ROADMAP.md` flip + auto-memory update + pin-bump commit
   (4 submodules; **no push without operator approval**).

All implementation coding/tests by `claude-opus-4-8` subagents; design/review/error-fixing by
`claude-fable-5` (per the operator's model-selection rules).

---

## File Changes

### `strategies/csm-set` (PR → `live-test`)

| File | Change |
|---|---|
| `src/csm/config/settings.py` | ADD `execution_mode`/`execution_account`/`execution_broker` + validators |
| `src/csm/execution/models.py` | NEW — wire mirrors + `OrderInstruction`/`SimPosition`/`SimPortfolio` |
| `src/csm/execution/engine_adapter.py` | NEW — `ExecutionEngineAdapter` (submit/get/stream, SSE parser, reconnect) |
| `src/csm/execution/sim_loop.py` | NEW — `build_order_instructions` + `run_sim_loop` |
| `src/csm/execution/errors.py` | EXTEND — adapter/stream/loop exception taxonomy under `ExecutionError` |
| `src/csm/execution/__init__.py` | EXTEND — re-exports |
| `scripts/verify_execution_sim.py` | NEW — operator verify CLI |
| `CLAUDE.md` | ADD "Execution mode (`CSM_EXECUTION_MODE`)" section |
| `.claude/knowledge/execution-mode.md` | NEW |
| `tests/unit/execution/test_models.py`, `test_engine_adapter.py`, `test_sim_loop.py` + settings tests | NEW/EXTEND |

### `strategies/tfex-s50-multi-tf-swing` (PR → `main`)

| File | Change |
|---|---|
| `src/tfex_s50_multi_tf_swing/config/settings.py` | ADD the `TFEX_S50_MULTI_TF_SWING_EXECUTION_*` trio + validators |
| `src/tfex_s50_multi_tf_swing/execution/models.py` | EXTEND — append wire mirrors (TFEX, `position_effect` required) + `SimPosition` + `infer_position_effect` |
| `src/tfex_s50_multi_tf_swing/execution/engine_adapter.py` | NEW — `STRATEGY_ID = "tfex-s50-multi-tf-swing"` |
| `src/tfex_s50_multi_tf_swing/execution/sim_loop.py` | NEW — instruction builder from `SetupSignal` + contracts; OPEN/CLOSE against the evolving position |
| `src/tfex_s50_multi_tf_swing/execution/errors.py` | EXTEND — same taxonomy under the existing `ExecutionError(TfexS50Error)` |
| `scripts/verify_execution_sim.py` | NEW — entry+exit in one invocation |
| `CLAUDE.md`, `.claude/knowledge/execution-mode.md` | ADD/NEW |
| `tests/unit/execution/*` + settings tests | NEW/EXTEND |

### `quant-api-gateway` (PR → `main`)

| File | Change |
|---|---|
| `src/api/v2/engines/execution.py` | `_proxy` + `_proxy_sse`: forward `X-Strategy-Id` when present |
| `tests/api/v2/test_engines_execution.py` | Forwarding asserted on POST + SSE; negative case |

### `quant-execution-engine` (this PR — docs only)

| File | Change |
|---|---|
| `docs/plans/phase5.1-strategy-execution-flags-sim-trade-loop.md` | NEW (this document) |
| `docs/plans/ROADMAP.md` | Phase 5.1 `[ ] Proposed` → `[x] Complete` (on completion) |
| `CLAUDE.md` | Current-state banner: Phase 5.1 complete; next Phase 6 (on completion) |
| `.claude/knowledge/order-update-stream.md` | APPEND "Strategy consumer contract" |

### Umbrella (commit only; push is a separate operator action)

| File | Change |
|---|---|
| `CLAUDE.md` | feature row / engine catalog / strategy rows: Phase 5.1 + flags |
| `.claude/knowledge/feature-execution-engine.md` | Phase 5.1 completion addendum |
| `plans/feature-execution-engine/ROADMAP.md` | Status flip |
| Submodule pins | csm-set, tfex, execution-engine, api-gateway |

---

## Success Criteria

- [ ] `CSM_EXECUTION_MODE=sim`: csm-set submits a `NormalizedOrder` via the gateway, receives
      SSE fill events, and updates its sim portfolio position (live verify evidence below).
- [ ] `TFEX_S50_MULTI_TF_SWING_EXECUTION_MODE=sim`: same loop with correct
      `position_effect=OPEN/CLOSE` and integer contracts.
- [ ] `EXECUTION_MODE=off` (default): zero-code path — no adapter instantiated, no HTTP call;
      existing internal sims unchanged.
- [ ] `EXECUTION_MODE=live` + `PUBLIC_MODE=true`: `ValueError` at `Settings()` construction.
- [ ] `execution.orders.strategy_id` stamped through the gateway (`csm-set` /
      `tfex-s50-multi-tf-swing`) — proves the gateway forwarding fix live.
- [ ] mypy strict, zero `type: ignore`; per-repo quality gates green; coverage ≥90% on the new
      execution modules; no new third-party dependencies.
- [ ] No `float` at any money boundary; no broker credential or broker code in any strategy.
- [ ] Four PRs merged + umbrella pins committed; all knowledge/CLAUDE.md files updated.

---

## Completion Notes

*(To be filled on completion: PR numbers/SHAs, per-repo test counts + coverage, live verify
evidence — cids, stream events, final positions, `strategy_id` stamp check — and the engine
restore-to-public confirmation.)*

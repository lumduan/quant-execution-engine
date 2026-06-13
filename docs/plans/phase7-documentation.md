# Phase 7: Documentation (tvkit-ref style, AI-agent-first)

**Feature:** quant-execution-engine — Phase 7: Documentation
**Branch:** `docs/phase7-documentation`
**Created:** 2026-06-13
**Status:** Complete
**Completed:** 2026-06-13
**Depends On:** Phase 6 (Complete, 2026-06-13)

---

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Spec Corrections (verified-code divergences)](#spec-corrections-verified-code-divergences)
6. [Implementation Steps](#implementation-steps)
7. [File Changes](#file-changes)
8. [Success Criteria](#success-criteria)
9. [Completion Notes](#completion-notes)

---

## Overview

### Purpose

Phase 7 is a **documentation-only** phase — no application code, test, or schema changes. The
execution engine has shipped Phases 0–6 (durable order store, engine core + `SimAdapter`,
`LiberatorAdapter` + `SettradeAdapter` real venues, per-market broker apps, the order-update
stream + dual-provider order book, and the safety/ops/reconciliation hardening). The one
remaining gap is a **documentation hub** that lets a strategy author or operator use the engine
without reading `src/`: every endpoint, every env var, and every state transition documented
with a real, working example.

The hub mirrors the `quant-marketdata-engine/docs/` precedent (tvkit-ref style, AI-agent-first):
a `README.md` table-of-contents hub linking `architecture/`, `api/`, `operations/`, and `data/`
sub-docs, each terse, table-first, and example-driven.

### Parent Plan Reference

- Per-service roadmap (canonical): [`docs/plans/ROADMAP.md`](ROADMAP.md) — Phase 7.
- Umbrella ADR (D1–D13, frozen contracts): [`../../../.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md).
- Umbrella cross-cutting roadmap: [`../../../plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md).
- Format precedent: [`../../../quant-marketdata-engine/docs/README.md`](../../../quant-marketdata-engine/docs/README.md).

### Key Deliverables

| File | Action | Description |
|------|--------|-------------|
| `docs/README.md` | CREATE | Documentation hub — one-line links to every sub-doc, conventions, cross-repo refs |
| `docs/overview.md` | REPLACE | One-paragraph service overview + pointers (was a placeholder) |
| `docs/architecture/overview.md` | CREATE | Topology, two planes (D1), gateway-proxy position, sole-credential-owner invariant, safety ladder, kill-switch, public mode |
| `docs/architecture/state-machine.md` | CREATE | The frozen 9-state / 13-edge order machine, terminal vs in-flight, append-only audit, idempotency, reconciliation window |
| `docs/architecture/adapters.md` | CREATE | `BrokerAdapter` interface, capability matrix, the two key findings, Sim/Liberator/Settrade adapter notes |
| `docs/architecture/security-boundary.md` | CREATE | Credential ownership, public vs owner mode, PTRM gate, price-band, kill-switch, logging-redaction rules |
| `docs/api/health.md` | CREATE | `GET /health` |
| `docs/api/orders-submit.md` | CREATE | `POST /orders` — `NormalizedOrder`, typed rejections, idempotency, stage gating |
| `docs/api/orders-get.md` | CREATE | `GET /orders/{client_order_id}` |
| `docs/api/orders-cancel.md` | CREATE | `DELETE /orders/{client_order_id}` (not kill-switch-gated) |
| `docs/api/orders-amend.md` | CREATE | `PATCH /orders/{client_order_id}` — native vs cancel+replace asymmetry |
| `docs/api/orders-stream.md` | CREATE | `GET /orders/stream` (SSE) — `Last-Event-ID` replay, filters, advisory nature |
| `docs/api/capabilities.md` | CREATE | `GET /capabilities` |
| `docs/api/order-book.md` | CREATE | `GET /order-book/{symbol}[/stream]` (default-off, D1 read-only) |
| `docs/api/admin.md` | CREATE | Owner-mode kill-switch + audit read/export |
| `docs/operations/bring-up.md` | CREATE | Three compose configs, health check, tear-down, schema prerequisite, fresh-clone gotcha |
| `docs/operations/configuration.md` | CREATE | Every `EXECUTION_ENGINE_*` env var (name/type/default/effect/SecretStr) |
| `docs/operations/kill-switch.md` | CREATE | Engage/disengage procedures, stage-flip rule, breaker relationship |
| `docs/operations/troubleshooting.md` | CREATE | Common failure modes + diagnosis + resolution |
| `docs/data/execution-schema.md` | CREATE | `db_execution` `orders`/`fills`/`order_events` schema, triggers, indexes, grants |
| `docs/data/state-machine-transitions.md` | CREATE | The verified 13-edge transition table |
| `.claude/knowledge/deployment.md` | CREATE | Compose topology (3 configs), Liberator internal port + redis, env-load order, fresh-clone |
| `.claude/knowledge/order-flow.md` | CREATE | End-to-end order path (gateway → engine → adapter → store → stream) |
| `.claude/playbooks/development-workflow.md` | CREATE | Quality gate, branch naming, bring-up, respx-mocked tests, Python 3.11 CI gotcha |
| `.claude/playbooks/order-routing-safety.md` | UPDATE | Ensure Phase 6 hardening covered (idempotent kill-switch, burst guard, per-account caps) |
| `../../.claude/playbooks/execution-engine-runbook.md` | CREATE | Umbrella operator runbook (cross-repo) |
| `CLAUDE.md` | UPDATE | Add Documentation section; flip Current-state Phase 7; update Where-to-look |
| `docs/plans/ROADMAP.md` | UPDATE | Mark Phase 7 `[ ]` → `[x]` with date + summary |
| `../../CLAUDE.md` (umbrella) | UPDATE | Confirm Phase 7 status reflected (minimal) |

---

## AI Prompt

The following prompt was used to generate this phase (reproduced verbatim):

````text
You are implementing **Phase 7 — Documentation (tvkit-ref style, AI-agent-first)** for the
`quant-execution-engine` sub-repo inside the `quant-trading-system` umbrella monorepo. This is a
**documentation-only phase** — no application code changes, no test changes, no schema changes.

[Sections 0–12 of the operator brief: model-selection rules; orientation reads; branch creation;
the plan-first requirement; the full documentation scope (docs/README.md hub; architecture/
overview + state-machine + adapters + security-boundary; api/ health, orders-submit/get/cancel/
amend/stream, capabilities, order-book, admin; operations/ bring-up, configuration, kill-switch,
troubleshooting; data/ execution-schema + state-machine-transitions); .claude/knowledge/
deployment + order-flow + reviews; .claude/playbooks/ development-workflow + order-routing-safety;
the umbrella execution-engine-runbook; the CLAUDE.md Documentation-section update; the umbrella
CLAUDE.md confirmation; the docs quality gate; and the commit/PR/pin-bump sequence) plus the
critical constraints: docs-only; do not invent endpoints/env vars (read src/ to verify); do not
document `live` as enabled (it is gated); no real credentials (placeholders only); no localhost
for in-container references; the D1 two-plane boundary is fundamental; kill-switch-first wherever
stage-flip appears; liberator-trading-api is internal-only.

The full operator brief is retained in the session transcript and the umbrella plan artifact
(`~/.claude/plans/you-are-implementing-phase-snug-flute.md`); this fenced block records that this
phase was prompt-driven and that the verified codebase — not the brief's hard-coded specifics —
is the source of truth (see "Spec Corrections" below).
````

> **Note on verbatim fidelity:** the operator brief embedded a hand-authored 13-edge state-machine
> table, several env-var names, an endpoint path, the amend-body fields, and a migration filename
> that **do not match the shipped code**. Per the brief's own constraint #12 ("do not invent …
> read `src/` to verify before documenting"), the docs follow the verified code. Every divergence
> is itemized in [Spec Corrections](#spec-corrections-verified-code-divergences).

---

## Scope

### In Scope

| Item | Status |
|------|--------|
| `docs/README.md` hub | [x] |
| `docs/overview.md` (replace placeholder) | [x] |
| `docs/architecture/{overview,state-machine,adapters,security-boundary}.md` | [x] |
| `docs/api/{health,orders-submit,orders-get,orders-cancel,orders-amend,orders-stream,capabilities,order-book,admin}.md` | [x] |
| `docs/operations/{bring-up,configuration,kill-switch,troubleshooting}.md` | [x] |
| `docs/data/{execution-schema,state-machine-transitions}.md` | [x] |
| `.claude/knowledge/{deployment,order-flow}.md` (new) | [x] |
| Review existing `.claude/knowledge/` (capability-matrix, contract, state-machine, decision-log, stream, order-book) | [x] |
| `.claude/playbooks/development-workflow.md` (new) | [x] |
| `.claude/playbooks/order-routing-safety.md` (Phase 6 refresh) | [x] |
| Umbrella `.claude/playbooks/execution-engine-runbook.md` (new) | [x] |
| `CLAUDE.md` Documentation section + Phase 7 state + Where-to-look | [x] |
| `docs/plans/ROADMAP.md` Phase 7 → complete | [x] |
| Umbrella `CLAUDE.md` Phase 7 confirmation | [x] |
| Docs quality gate (links / secrets / localhost / curl / env-var coverage) | [x] |

### Out of Scope

- Any change to `src/`, `tests/`, `pyproject.toml`, compose files, or `quant-infra-db` SQL.
- New endpoints, env vars, order types, or state-machine edges (docs reflect what shipped).
- Documenting `live` as enabled — it stays gated; the docs state the prerequisites only.
- The full tvkit 7-category doc tree (getting-started/concepts/guides/reference) — the focused
  core (architecture/api/operations/data) ships now; the rest is a later `Phase 7.x` if desired.

---

## Design Decisions

### 1. Mirror the market-data-engine `docs/` layout exactly

The umbrella already has one shipped docs hub (`quant-marketdata-engine/docs/`). Re-using its
structure (hub `README.md` → `architecture/` + `api/` + `operations/` + `data/`), its API-doc
template (metadata table → params/body table → engine-direct **and** gateway curl → response JSON +
field table → errors → notes), and its conventions block (Decimal-as-string, UTC, placeholder
secrets, in-container hostnames) keeps the two engines' docs consistent for both humans and agents.

### 2. The execution engine's docs are denser than market-data's

Execution has more surface area — two real broker adapters, native amend, the kill-switch, two SSE
streams, a dual-provider order book, an admin/audit surface. So `architecture/` gets **four** docs
(vs three), `api/` gets **nine** (vs five), and `data/` gets a dedicated transition-table doc. This
is a deliberate divergence from the precedent's file count, justified by surface area.

### 3. Keep `docs/overview.md` AND add `docs/README.md`

Matching the precedent: `README.md` is the link hub; `overview.md` is a one-paragraph orientation
that the hub links to. The existing `overview.md` was a scaffold placeholder and is replaced.

### 4. The capability matrix lives in two depths

`contracts/capabilities.py` exposes a narrow `CapabilitySet` (broker, market, order_types, tifs,
position_effects, amend, adapter_installed) — that is exactly what `GET /capabilities` returns, so
`api/capabilities.md` documents that shape. The **broader** matrix (auth model, cancel, query,
order-update stream, client-idempotency-key) lives in `.claude/knowledge/capability-matrix.md`; the
denser `architecture/adapters.md` reproduces that broader table.

### 5. Source-of-truth verification over brief fidelity

The brief is prescriptive but contains several values that diverge from the shipped code. The docs
follow the verified code (constraint #12); divergences are recorded transparently in the next
section and in the PR body (operator decision, 2026-06-13).

---

## Spec Corrections (verified-code divergences)

Read directly from `src/` and `quant-infra-db/init-scripts/`. The docs document the **right-hand
(verified)** value in every case.

| # | Brief said | Verified code | Source |
|---|-----------|---------------|--------|
| 1 | State machine has `NEW→CANCELLED`, `PARTIALLY_FILLED→CANCELLED`, and a `(any non-terminal)→CANCELLED` kill-switch edge | **No** direct `*→CANCELLED` edges. Cancel is two-step `…→PENDING_CANCEL→CANCELLED`; kill-switch mass-cancel reuses the ordinary cancel path. Real edges the brief omitted: `NEW→EXPIRED`, `PARTIALLY_FILLED→EXPIRED`, `NEW→PENDING_CANCEL`, `PARTIALLY_FILLED→PENDING_CANCEL`, `PARTIALLY_FILLED→PENDING_REPLACE` | `core/state_machine.py` `LEGAL_EDGES` |
| 2 | Env var `DB_DSN` | **`PG_DSN`** (`EXECUTION_ENGINE_PG_DSN`) | `config/settings.py` |
| 3 | Env var `RISK_ORDER_RATE_LIMIT_PER_SECOND` | **`RISK_MAX_ORDERS_PER_SECOND`** (default 5) | `config/settings.py` |
| 4 | Env vars `API_HOST` / `API_PORT` | Do **not** exist — only `HOST_PORT` (default 8400, informational; uvicorn binds container `:8000`) | `config/settings.py` |
| 5 | Status route `GET /admin/kill-switch/status` | **`GET /admin/kill-switch`** (no `/status`) | `api/routes.py` |
| 6 | Amend body `new_price?, new_quantity?, new_display_qty?` | **`new_price`, `new_qty`, `new_client_order_id`** (UUIDv4, cancel_replace only); no `new_quantity`/`new_display_qty` | `api/schemas.py::AmendOrderRequest` |
| 7 | Migration file `15_execution_strategy_id.sql` | **`13_execution_strategy_id.sql`** (#15 was the PR number); base schema `12_schema_execution.sql` | `quant-infra-db/init-scripts/` |
| 8 | `CapabilityNotSupported` → 400 | `CapabilityError` (`capability_unsupported`) → **422** | `api/error_handlers.py` |
| 9 | `GET /capabilities` is "public, no auth required" | api-key-gated (`require_api_key`) but **public-mode-readable** (allowed in public mode) | `api/routes.py`, `api/deps.py` |

Plus real env vars the brief omitted and the docs add: `APP_ENV`, `LOG_LEVEL`, `PG_POOL_MIN_SIZE`,
`PG_POOL_MAX_SIZE`, `API_KEY`, `RISK_DUPLICATE_BURST_WINDOW_SECONDS` (legacy, no longer read),
`SUBMIT_LOCK_TTL_SECONDS`, `SUBMIT_LOCK_WAIT_MS`, `SIM_DEFAULT_FILL_PRICE`, `SETTRADE_ACCOUNT_NO`,
`ORDER_BOOK_LIBERATOR_EXTRA_CA_PEM`.

---

## Implementation Steps

1. Branch `docs/phase7-documentation` off `main` (done).
2. Write + commit this plan doc (`docs(plans): add Phase 7 documentation implementation plan`).
3. `docs/architecture/*` (overview, state-machine, adapters, security-boundary).
4. `docs/api/*` (9 endpoint docs) + `docs/README.md` hub + `docs/overview.md`.
5. `docs/operations/*` (bring-up, configuration, kill-switch, troubleshooting).
6. `docs/data/*` (execution-schema, state-machine-transitions).
7. `.claude/knowledge/{deployment,order-flow}.md`; review the existing knowledge files.
8. `.claude/playbooks/development-workflow.md` (new); refresh `order-routing-safety.md`.
9. Umbrella `.claude/playbooks/execution-engine-runbook.md`.
10. `CLAUDE.md` Documentation section + Phase 7 state; umbrella `CLAUDE.md` confirm.
11. Docs quality gate (links resolve, no secrets, no bare localhost, curl per endpoint, env-var coverage).
12. Commit (`docs(phase7): documentation hub …`), mark complete (ROADMAP + plan Completion Notes + CLAUDE.md),
    push, open PR (with the Spec-corrections list in the body), then bump the umbrella pin.

---

## File Changes

See [Key Deliverables](#key-deliverables) — all files there, with Action.

---

## Success Criteria

Matches ROADMAP Phase 7 acceptance:

- [x] Every endpoint (`/health`, `POST/GET/DELETE/PATCH /orders`, `/orders/stream`, `/capabilities`,
      `/order-book/{symbol}[/stream]`, `/admin/kill-switch[/engage|/disengage]`,
      `/admin/orders/{cid}/audit`, `/admin/audit/export`) has a real curl example.
- [x] Every `EXECUTION_ENGINE_*` env var documents type / default / allowed / effect (+ SecretStr flag).
- [x] The order state machine + capability matrix are documented (verified 13 edges; 6 capability rows).
- [x] No secrets — SecretStr examples use `<your-value-here>`; the public-safe scan is clean.
- [x] All internal links resolve.
- [x] `live` is documented as gated, with prerequisites; the D1 two-plane boundary is preserved;
      kill-switch-first is stated wherever stage-flip appears; `liberator-trading-api` is internal-only.

---

## Completion Notes

### Summary

Phase 7 shipped the execution-engine documentation hub (tvkit-ref style, AI-agent-first) — docs-only,
no application code / test / schema change. Delivered: `docs/README.md` + `docs/overview.md`;
`architecture/` ×4; `api/` ×9 (each with a real curl example, engine-direct + gateway); `operations/`
×4 (incl. every `EXECUTION_ENGINE_*` env var with type / default / effect / SecretStr); `data/` ×2
(the schema + the verified 13-edge table). Added `.claude/knowledge/{order-flow,deployment}.md`, a new
`.claude/playbooks/development-workflow.md`, a Phase-6 refresh of `order-routing-safety.md`, and the
umbrella `.claude/playbooks/execution-engine-runbook.md`. Updated `CLAUDE.md` (Documentation section +
Phase 7 state + Where-to-look), the umbrella `CLAUDE.md` Phase 7 status, and marked Phase 7 complete
in the ROADMAP.

### Verified-code corrections (constraint #12)

The brief's hard-coded specifics were corrected to the shipped code: the 13 state edges (no direct
`*→CANCELLED`; cancel is two-step; the kill-switch reuses the cancel path); `PG_DSN` not `DB_DSN`;
`RISK_MAX_ORDERS_PER_SECOND` not `RISK_ORDER_RATE_LIMIT_PER_SECOND`; no `API_HOST`/`API_PORT` (only
`HOST_PORT`); `GET /admin/kill-switch` (no `/status`); amend body `new_qty`/`new_client_order_id` (no
`new_quantity`/`new_display_qty`); migration `13_execution_strategy_id.sql` (not `15_*`);
`capability_unsupported` is 422 (not 400); a disabled order book returns 404 (not 503). The submit
pipeline order was also taken from `router.py` (kill-switch → dedupe → capability → PTRM incl. the
duplicate-burst guard → price-band → stage → adapter). The existing `.claude/knowledge/`
contract / matrix / state-machine / decision-log / stream / order-book docs were reviewed and left
unchanged (accurate + current).

### Quality gate

Docs-only: internal links spot-checked, the public-safe secret scan is clean, no bare `localhost` in
in-container examples, every endpoint has ≥1 curl example, every env var documents
type / default / effect. No `ruff` / `mypy` / `pytest` — no source was touched.

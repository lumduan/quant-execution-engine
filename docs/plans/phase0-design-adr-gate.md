# Phase 0: Design & ADR gate — freeze the contracts

**Feature:** `feature-execution-engine` — Phase 0: Design & ADR gate
**Branch:** `docs/phase0-adr-gate`
**Created:** 2026-06-10
**Status:** Complete
**Completed:** 2026-06-10
**Depends On:** Repo scaffold + knowledge seed (PRs #1–#2, merged 2026-06-09)

---

## Table of Contents

1. [Overview](#overview)
2. [Prompt](#prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Implementation Steps](#implementation-steps)
6. [File Changes](#file-changes)
7. [Success Criteria](#success-criteria)
8. [Verification](#verification)
9. [Rollback](#rollback)
10. [Completion Notes](#completion-notes)

---

## Overview

### Purpose

Phase 0 is the **design/ADR gate** for the whole execution-engine feature: it ships
**documentation, not code**. It promotes the umbrella ADR stub
([`.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md))
to a full **ACCEPTED** ADR — confirming Decision Log **D1–D13**, freezing the
`NormalizedOrder` / `NormalizedOrderResult` contract + `NormalizedStatus` enum, the
`BrokerAdapter` interface signature, the order state machine (states + complete
legal-transition table), and the capability-matrix shape — and pins every item in the
ROADMAP's "Open questions / risks" list as a written decision. The sub-repo knowledge seed
(contract, state machine, capability matrix, decision log, broker research) is brought into
exact agreement with the frozen ADR, and Phase 0 statuses are flipped across both repos so
Phase 1 (the `quant-infra-db` `execution` schema) is unblocked.

### Parent Plan Reference

- Per-service roadmap: [`docs/plans/ROADMAP.md`](ROADMAP.md) — section "Phase 0 — Design & ADR gate"
- Cross-cutting roadmap: [`plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md)
- The ADR (the gate artifact this phase promotes):
  [`.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md) (umbrella)

### Key Deliverables

1. **This plan file** — `docs/plans/phase0-design-adr-gate.md` (prompt embedded below).
2. **Sub-repo knowledge-seed freeze** — `.claude/knowledge/{decision-log,normalized-order-contract,order-state-machine,capability-matrix}.md`
   confirmed against the ADR and marked frozen; ADR cross-links both directions.
3. **ROADMAP Phase 0 status flip** — `docs/plans/ROADMAP.md` Phase 0 `[x]` (2026-06-10),
   open questions annotated **PINNED**.
4. **Sub-repo `CLAUDE.md` status refresh** — current-state callout reflects the accepted ADR.
5. *(Umbrella PR, separate)* — the promoted ADR, cross-cutting ROADMAP/registry/`CLAUDE.md`
   status updates, and the submodule pin bump after this PR merges.

---

## Prompt

The following prompt drove this phase:

```text
# Task: quant-execution-engine — Phase 0 "Design & ADR gate" (documentation-only phase)

  ## Role

  You are a senior platform engineer working in the `quant-trading-system` umbrella repo
  (`/home/batt/docker/quant-trading-system`), a meta-repo of version-locked **git submodules**.
  The target sub-repo is `quant-execution-engine/` (its own git repo, remote
  `github.com/lumduan/quant-execution-engine`, integration branch `main`). Phase 0 is a
  **design/ADR phase — it ships documentation, not code**. Do not build adapters, schemas,
  routes, or anything that touches a broker.

  ## Required reading (in this order, before any edit)

  1. `CLAUDE.md` (umbrella) — submodule rules, network contract, cross-cutting rules.
  2. `quant-execution-engine/CLAUDE.md` — per-repo agent context and conventions.
  3. `quant-execution-engine/docs/plans/ROADMAP.md` — read the whole file; Phase 0 is the
     section "Phase 0 — Design & ADR gate", and the "Open questions / risks (pin in Phase 0)"
     section at the bottom is part of your scope.
  4. The umbrella ADR stub: `.claude/knowledge/feature-execution-engine.md` (this is the
     artifact Phase 0 promotes to a full ADR).
  5. The existing knowledge seed in `quant-execution-engine/.claude/knowledge/`
     (`decision-log.md`, `normalized-order-contract.md`, `order-state-machine.md`,
     `capability-matrix.md`, `broker-research-liberator.md`, `broker-research-settrade.md`,
     `architecture.md`) — Phase 0 *confirms and freezes* these, it does not rewrite them
     from scratch.
  6. Plan-format reference: `strategies/csm-set/docs/plans/examples/phase1-sample.md`.

  ## Hard constraints (submodule discipline)

  - All sub-repo work happens **inside `quant-execution-engine/` on a feature branch** with
    its own commit/PR. Before editing, `git -C quant-execution-engine fetch && git -C
    quant-execution-engine switch main && git pull`, then create a branch (suggested:
    `docs/phase0-adr-gate`). Never commit on a detached HEAD.
  - Umbrella-side edits (the ADR in `.claude/knowledge/`, `CLAUDE.md`, `plans/feature-execution-engine/ROADMAP.md`)
    happen **in the umbrella repo on its own branch** (suggested: `docs/phase0-execution-adr`)
    with a separate PR. Never commit umbrella changes that reach inside the submodule's history.
  - If the sub-repo PR merges and you bump the umbrella pin, that is an explicit
    `git add quant-execution-engine` + `chore: bump quant-execution-engine pin to <sha>` commit
    in the umbrella PR — pins never move implicitly.
  - `quant-execution-engine` contains a nested private submodule
    `third_party/liberator-trading-api` — do **not** touch it in this phase.

  ## Step 1 — Write the implementation plan first

  Before changing any other file, author
  `quant-execution-engine/docs/plans/phase0-design-adr-gate.md` following the structure of
  `strategies/csm-set/docs/plans/examples/phase1-sample.md` (objective, scope, non-goals,
  step-by-step work items with checkboxes, acceptance criteria, verification, rollback).
  **Embed this entire prompt verbatim in a "Prompt" section of that plan file.** The plan is
  committed as part of the phase PR.

  ## Step 2 — Phase 0 scope (from the ROADMAP; treat as the spec)

  1. **Promote the ADR.** Upgrade umbrella `.claude/knowledge/feature-execution-engine.md`
     from stub to full ADR: confirm Decision Log **D1–D13** (status: each decision Accepted /
     Amended-with-rationale / explicitly Rejected — no decision left "Proposed"), and pin:
     - the `NormalizedOrder` / `NormalizedOrderResult` field set + `NormalizedStatus` enum
       (the ROADMAP sketch at the top of `quant-execution-engine/docs/plans/ROADMAP.md` is
       the starting point; `Decimal`-as-string on the wire, `int` quantities, UTC timestamps,
       no `float` at money boundaries),
     - the `BrokerAdapter` interface signature
       (`place`/`cancel`/`amend`/`get_open_orders`/`get_positions`/`get_account`/`capabilities`),
     - the order state machine: states + the complete legal-transition table,
     - the capability-matrix shape (per `(broker, market)` capability sets, including the
       Liberator amend = cancel+replace non-atomic semantic).
  2. **Pin the open questions.** Resolve each item in the ROADMAP's "Open questions / risks
     (pin in Phase 0)" list into a written decision in the ADR / decision log — most
     importantly the delivery guarantee (**dedupe + reconcile + idempotent re-submit**, not
     exactly-once) and the `client_order_id ↔ broker_order_id` mapping rule (persist
     atomically with `PENDING_NEW → NEW`; reconciliation fallback match on
     `(account, symbol, side, qty, ts-window)`).
  3. **Verify/complete umbrella registration.** The service is already registered in the
     umbrella `CLAUDE.md` (repo table, network contract `:8400`, engine catalog, bring-up,
     health checks) — audit those sections for drift against the final ADR and fix only what
     is inconsistent; do not duplicate.
  4. **Confirm the per-repo knowledge seed.** Bring
     `quant-execution-engine/.claude/knowledge/*` (contract, state machine, capability
     matrix, decision log, broker research) into exact agreement with the frozen ADR — the
     ADR is the source of truth; cross-link both directions.
  5. **Flip statuses.** Mark Phase 0 done (`[x]`, date 2026-06-10, summary of what shipped)
     in `quant-execution-engine/docs/plans/ROADMAP.md`, and update the cross-cutting
     `plans/feature-execution-engine/ROADMAP.md` Phase 0 entry plus the
     `feature-execution-engine` rows/blurbs in umbrella `CLAUDE.md` and
     `.claude/knowledge/optional-features-registry.md` so status text ("gated on the Phase 0
     ADR") reflects the gate being passed and Phase 1 being unblocked.

  ## Step 3 — Knowledge / memory / playbook hygiene

  Where Phase 0 produced durable, non-derivable knowledge, create or update:
  - `quant-execution-engine/CLAUDE.md` and `quant-execution-engine/.claude/*` (per-repo), and
  - umbrella `CLAUDE.md` and `.claude/*` (cross-cutting only — e.g. the ADR, the
    optional-features registry, a cross-repo playbook if the Phase 1→2 PR sequencing needs one).
  Keep the umbrella free of service-internal detail; one fact, one home, with links instead
  of copies.

  ## Non-goals (reject scope creep)

  No Python code beyond what already exists in the scaffold; no broker calls; no DB schema
  (that is Phase 1, a `quant-infra-db` PR); no routes beyond the existing `/health`; no
  gateway changes (Phase 2); no edits to `third_party/liberator-trading-api`.

  ## Quality gate & verification

  - Even though this is docs-only, run the sub-repo's full gate before pushing
    (`uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`)
    to prove the scaffold stays green — if any later `sed`/Edit touches a checked file,
    re-run `ruff format --check` before push.
  - Markdown: relative links must resolve from each file's location (umbrella↔sub-repo links
    cross a repo boundary — verify each one); tables render; no decision left "TBD";
    every D1–D13 entry has a status and a one-line rationale.
  - No secrets, account numbers, broker credentials, or cookie material in any committed file.

  ## Deliverables / PR flow

  1. **Sub-repo PR** (`lumduan/quant-execution-engine`, branch `docs/phase0-adr-gate` → `main`):
     the plan file `docs/plans/phase0-design-adr-gate.md` (with embedded prompt), knowledge-seed
     confirmations, ROADMAP Phase 0 status flip. Conventional-commit messages
     (`docs(phase0): …`).
  2. **Umbrella PR** (`lumduan/quant-trading-system`, branch `docs/phase0-execution-adr` → `main`):
     the promoted ADR, registry/ROADMAP/CLAUDE.md status updates, and — after PR 1 merges —
     the submodule pin bump commit.
  3. Open both PRs with `gh pr create`, each body containing: summary, decision highlights
     (the D1–D13 outcome in one table), verification evidence (gate output), and a checklist
     mapping to the Phase 0 acceptance criteria from the ROADMAP.
  4. After every commit/push/PR, report results as an ASCII box-drawing table with columns
     `Repo | Branch | Commit | GitHub` (box-drawing characters, one row per repo).

  ## Acceptance (from the ROADMAP — all must hold)

  ADR merged with D1–D13 accepted; `NormalizedOrder` contract + state machine + capability
  matrix frozen and mutually consistent across ADR and sub-repo knowledge; open
  questions/risks pinned in writing; service registration in the umbrella verified; scaffold
  quality gate green; both PRs open (or merged) with the pin-bump sequenced after the
  sub-repo merge.
```

---

## Scope

### In Scope (Phase 0)

| Component | Description | Status |
|---|---|---|
| This plan file | `docs/plans/phase0-design-adr-gate.md` with the prompt embedded | Complete |
| ADR promotion *(umbrella PR)* | `feature-execution-engine.md` stub → full ACCEPTED ADR: D1–D13 confirmed, contract + interface + state machine + capability-matrix shape pinned | Complete |
| Open-question pinning | All 7 "Open questions / risks (pin in Phase 0)" items resolved as written decisions (ADR Pinned §A–§G) | Complete |
| Knowledge-seed freeze | `decision-log.md`, `normalized-order-contract.md`, `order-state-machine.md`, `capability-matrix.md` confirmed against the ADR, marked frozen, cross-linked both directions | Complete |
| ROADMAP status flip | Phase 0 `[x]` (2026-06-10) in this repo + the cross-cutting ROADMAP *(umbrella PR)* | Complete |
| Umbrella registration audit | Repo table / network contract `:8400` / engine catalog / bring-up / health checks checked for drift vs the ADR — status text updated only where stale *(umbrella PR)* | Complete |
| Registry + `CLAUDE.md` status text | "gated on the Phase 0 ADR" → gate passed, Phase 1 unblocked *(umbrella PR + this repo's `CLAUDE.md`)* | Complete |

### Out of Scope (rejected scope creep)

- Any Python code beyond the existing scaffold; no broker calls.
- The `execution` DB schema — **Phase 1**, a `quant-infra-db` PR.
- Routes beyond the existing `/health`; gateway proxy changes — **Phase 2**.
- Adapter work, Settrade venue-enum pinning (`(confirm P4)` cells) — **Phases 3/4**.
- Any edit to the nested submodule `third_party/liberator-trading-api`.
- A new umbrella playbook for Phase 1→2 PR sequencing — deliberately **not** created: the
  dependency-ordered sequence already lives in both ROADMAPs; a playbook would duplicate it.

---

## Design Decisions

The substantive decisions are recorded **once**, in the ADR
([`.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md),
umbrella). Summary of what Phase 0 pinned:

1. **D1–D13 all ACCEPTED as drafted** — no amendment, no rejection, no new D-number. D5's
   "exactly-once-ish" wording is clarified (not amended) by the delivery guarantee below.
2. **Delivery guarantee (ADR §A):** **at-least-once submission + engine-side dedupe on
   `client_order_id` + durable state + reconciliation + idempotent re-submit — explicitly
   NOT exactly-once.** Neither broker accepts a client idempotency key (finding R1), so true
   exactly-once is unattainable; reconciliation never blindly re-sends. `client_order_id`
   generation standard: **UUIDv4** (client-generated, format-validated, opaque to the engine;
   time-ordered schemes — ULID/UUIDv7/Snowflake — are acceptable drop-ins because the id is
   never parsed for time).
3. **Id-mapping rule (ADR §B):** the `client_order_id ↔ broker_order_id` mapping is persisted
   **atomically with the `PENDING_NEW → NEW` transition**; if the ack is lost, reconciliation
   fuzzy-matches on `(account, symbol, side, qty)` within **±5 s** of the persisted submit
   timestamp to recover the broker id — bounded resolution, never blocking routing
   indefinitely.
4. **Contract frozen (ADR §C–§D):** `NormalizedOrder` / `NormalizedOrderResult` field sets,
   `NormalizedStatus` enum, and the 7-method `BrokerAdapter` interface are frozen exactly as
   seeded ([`normalized-order-contract.md`](../../.claude/knowledge/normalized-order-contract.md)).
   `Decimal`-as-string on the wire; `int` quantities; UTC timestamps; no `float` at money
   boundaries.
5. **State machine frozen (ADR §E):** 9 states (3 local, 2 venue, 4 terminal) + the complete
   legal-transition table exactly as seeded
   ([`order-state-machine.md`](../../.claude/knowledge/order-state-machine.md)).
6. **Capability-matrix shape frozen (ADR §F):** per-`(broker, market)` capability sets
   enforced by the router pre-venue; Liberator amend = cancel+replace declared **non-atomic**;
   `(confirm P4)` Settrade venue enums stay deferred-by-design (finding R4). The full matrix
   stays canonical in [`capability-matrix.md`](../../.claude/knowledge/capability-matrix.md)
   — the ADR links it rather than copying it.
7. **Remaining risks pinned (ADR §G):** order-type semantics drift (enum frozen now; each
   `(broker, market, order_type)` combination is a **distinct pre-flight validation class** —
   Phase 3/4 adapter work), auth liveness (adapter-local per D10; a **proactive heartbeat
   worker** polls a low-impact read every **~30 s** per adapter and consecutive failures trip
   a **circuit breaker** halting that broker's routing + raising an alert — waiting for a 401
   on a live order is unacceptable), real-money blast radius (PTRM caps — max order
   value/qty, per-second rate limit — plus the global kill-switch **(reject new + mass-cancel
   open)** and `sim`-default stage are **Phase 2 milestones**, per E2/E3/D11), and streaming
   creep (D1 reaffirmed — market-data / order-book streams are strictly external, read-only
   dependencies).

All seven stances + parameters (UUIDv4, ±5 s window, ~30 s heartbeat + circuit breaker,
kill-switch mass-cancel, PTRM in Phase 2) were **confirmed by the owner on 2026-06-10**
during this phase.

---

## Implementation Steps

### Sub-repo PR (`docs/phase0-adr-gate` → `main`, this repo)

- [x] Sync `main`, create branch `docs/phase0-adr-gate` (no detached-HEAD commits).
- [x] Author this plan file **first**, prompt embedded verbatim.
- [x] `docs/plans/ROADMAP.md` — top status line + Phase 0 `[x]` (2026-06-10) + contract
      heading "(sketch — pinned in Phase 0/2)" → frozen; annotate all 7 open questions
      **PINNED** with ADR pointers.
- [x] `.claude/knowledge/decision-log.md` — D1–D13 confirmed Accepted 2026-06-10; add the
      Phase 0 pinned-resolutions pointer list; cross-link the ADR as source of truth.
- [x] `.claude/knowledge/normalized-order-contract.md` — mark FROZEN; add the `BrokerAdapter`
      interface signature; link the ADR.
- [x] `.claude/knowledge/order-state-machine.md` — mark FROZEN; transition table verified
      identical to the ADR.
- [x] `.claude/knowledge/capability-matrix.md` — mark shape FROZEN; link the ADR.
- [x] `CLAUDE.md` — current-state callout → Phase 0 complete (ADR accepted 2026-06-10),
      Phase 1 next; contract heading wording.
- [x] Run the full quality gate; commit `docs(phase0): …`; push; open PR 1.

### Umbrella PR (`docs/phase0-execution-adr` → `main`, umbrella repo — separate)

- [x] Promote `.claude/knowledge/feature-execution-engine.md` to the full ACCEPTED ADR
      (D1–D13 table + Pinned §A–§G + consequences + cross-references).
- [x] `plans/feature-execution-engine/ROADMAP.md` — status flip, Phase 0 checkboxes `[x]`,
      open questions pinned, stale cross-references fixed.
- [x] Umbrella `CLAUDE.md` + `.claude/knowledge/optional-features-registry.md` — status text
      reflects the gate passed and Phase 1 unblocked; registration sections audited.
- [x] After PR 1 merges: `git add quant-execution-engine` +
      `chore: bump quant-execution-engine pin to <sha>`; open PR 2.

---

## File Changes

| File | Action | Description |
|---|---|---|
| `docs/plans/phase0-design-adr-gate.md` | CREATE | This plan (prompt embedded) |
| `docs/plans/ROADMAP.md` | MODIFY | Phase 0 `[x]`; status line; open questions → PINNED |
| `.claude/knowledge/decision-log.md` | MODIFY | D1–D13 confirmed; pinned-resolutions pointers |
| `.claude/knowledge/normalized-order-contract.md` | MODIFY | FROZEN marker; `BrokerAdapter` signature |
| `.claude/knowledge/order-state-machine.md` | MODIFY | FROZEN marker; ADR cross-link |
| `.claude/knowledge/capability-matrix.md` | MODIFY | Shape-FROZEN marker; ADR cross-link |
| `CLAUDE.md` | MODIFY | Current-state callout → Phase 0 complete |
| *(umbrella)* `.claude/knowledge/feature-execution-engine.md` | MODIFY | Stub → full ACCEPTED ADR |
| *(umbrella)* `plans/feature-execution-engine/ROADMAP.md` | MODIFY | Phase 0 flip + drift fixes |
| *(umbrella)* `CLAUDE.md` | MODIFY | Status text only (registration audited, no drift) |
| *(umbrella)* `.claude/knowledge/optional-features-registry.md` | MODIFY | Row status update |

---

## Success Criteria

(= ROADMAP Phase 0 acceptance.)

- [x] ADR promoted with **D1–D13 accepted** — every entry has a status + one-line rationale;
      nothing left "Proposed"/"TBD".
- [x] `NormalizedOrder` contract + `NormalizedStatus` enum + `BrokerAdapter` signature +
      order state machine + capability-matrix shape **frozen and mutually consistent** across
      the ADR and this repo's knowledge seed.
- [x] All "Open questions / risks (pin in Phase 0)" items pinned in writing — including the
      delivery guarantee (dedupe + reconcile + idempotent re-submit, **not** exactly-once)
      and the `client_order_id ↔ broker_order_id` mapping rule.
- [x] Service registration in the umbrella `CLAUDE.md` verified (no structural drift found;
      status text updated).
- [x] Scaffold quality gate green (ruff check + format + mypy strict + pytest ≥90%).
- [x] Both PRs open/merged with the pin bump sequenced **after** the sub-repo merge.

---

## Verification

- Full gate (matches CI):
  `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest`
  — must stay green (docs-only change; the scaffold's 100%-coverage suite must not regress).
- Relative-link audit on every added/edited markdown file, including the cross-repo links
  (`../../../.claude/knowledge/…` from this repo's docs resolve only inside the umbrella
  checkout — same convention the ROADMAP already uses).
- `grep` audit of the ADR: no "TBD"; no D-decision left "Proposed".
- Secrets scan of the diff: no PIN / token / account number / cookie material.

## Rollback

Docs-only phase. Rollback = revert the sub-repo PR and the umbrella PR (the pin bump reverts
with the umbrella PR). No code path, schema, or runtime behaviour is affected.

---

## Completion Notes

### Summary

Phase 0 shipped as two coordinated docs-only PRs. The umbrella ADR was promoted to ACCEPTED
(D1–D13 confirmed as drafted; Pinned §A–§G resolve every open question), the knowledge seed
in this repo was confirmed and marked frozen against it, and Phase 0 statuses were flipped in
both ROADMAPs, the umbrella `CLAUDE.md`, and the optional-features registry. The umbrella
registration audit found **no structural drift** — only status text ("gated on the Phase 0
ADR") needed updating. Phase 1 (`quant-infra-db` `execution` schema) is unblocked.

### Issues Encountered

1. **Stale cross-references in the cross-cutting ROADMAP** — it still said the ADR was
   "*(to be authored in Phase 0)*" and the service repo "*(created in Phase 2)*"; both
   predated the 2026-06-09 scaffold and were fixed in the umbrella PR.
2. **Owner alignment arrived mid-phase** (2026-06-10) confirming all seven open-question
   stances and adding concrete parameters (UUIDv4 id standard, ±5 s reconciliation window,
   ~30 s heartbeat + circuit breaker, kill-switch mass-cancel, PTRM as Phase 2 milestones) —
   folded into the ADR §A–§G and both ROADMAPs in this same PR.

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Fable 5)
**Status:** Complete
**Completed:** 2026-06-10

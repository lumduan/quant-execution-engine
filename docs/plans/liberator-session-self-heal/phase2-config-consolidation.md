# Phase 2: Config Consolidation

**Feature:** Liberator session self-heal — Phase 2: Config consolidation
**Branch:** `feat/liberator-session-self-heal-phase2-config-consolidation`
**Created:** 2026-06-14
**Status:** Complete
**Completed:** 2026-06-14
**Depends On:** [`phase1-liveness-probe-correctness.md`](phase1-liveness-probe-correctness.md) (Complete); [`ROADMAP.md`](ROADMAP.md) Phase 2; decisions **D5**, **D6**

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

The bundled `third_party/liberator-trading-api` shipped **confusing, duplicate** session-monitor
configuration. The ROADMAP (and the originating prompt) framed Phase 2 as collapsing **two
Pydantic schemas** — `session_status.monitoring` (schema A) and `session_monitor` (schema B) —
into one `SessionMonitorConfig`. **Verification against the code showed that framing is
inaccurate** (see Design Decisions §1): there is only **one** monitor model, already with a single
`enabled` + single cadence; `session_status.monitoring` (and `session_status.health_check`) are
**dead YAML** that no service reads. So Phase 2 is really a **config-file cleanup** — make every
shipped config speak the one schema the code actually consumes — plus the completion of the
Phase-1 breadcrumb (remove the now-dead `SessionStatusTestPayload`).

**The monitor stays DISABLED after this phase** (Phase 5 enables it). No logic change.

### Parent Plan Reference

- [`docs/plans/liberator-session-self-heal/ROADMAP.md`](ROADMAP.md) — feature roadmap (D5, D6)
- Engine ROADMAP: [`docs/plans/ROADMAP.md`](../ROADMAP.md)
- Cross-cutting: [`plans/feature-execution-engine/ROADMAP.md`](../../../../plans/feature-execution-engine/ROADMAP.md)

### Key Deliverables

1. **One consumed schema in every config** — `session_monitor` (→ `SessionMonitorConfig`) +
   `session_status.config` / `.health` (→ `SessionStatusConfig` / `SessionStatusHealthConfig`);
   the dead `session_status.monitoring` / `health_check` blocks are gone.
2. **Sample-footgun fix** — `config/session_status.sample.yaml.example` (the `setup.py`
   provisioning source) had been **missing `session_monitor` entirely** → a generated config would
   default the monitor `enabled=True`. It now carries the **disabled** block.
3. **Dead-model removal** — drop `SessionStatusTestPayload` + `SessionStatusResponse.test_payload_used`
   (always `None` since Phase 1).
4. **Config-parse test** — `tests/test_models/test_session_status_config.py` asserts every shipped
   example parses into `SessionMonitorConfig`, ships disabled, round-trips, and no dead block reappears.
5. **Mounted config migrated** — `docker/liberator/session_status.yaml` to the consumed schema,
   monitor still disabled.
6. **ROADMAP** — Phase 1 + Phase 2 marked complete; **submodule pin bump** (D6).

---

## AI Prompt

The following prompt was used to generate this phase (verbatim):

```
You are a senior Python/FastAPI engineer implementing Phase 2 — Config consolidation of the
liberator-session-self-heal feature inside the quant-execution-engine submodule ecosystem.

0. Orientation — read CLAUDE.md (umbrella), quant-execution-engine/CLAUDE.md, the feature ROADMAP,
   the csm-set phase1-sample plan-format reference, the current session_status.py duplicate-schema
   state, the current example config, and the current mounted engine config.

1. ROADMAP housekeeping — mark Phase 1 [x] (complete), set Phase 2 [~] (in progress); commit the
   ROADMAP update with the other engine-repo changes (not a separate commit).

2. Branch setup — submodule first (D6): branch
   feat/liberator-session-self-heal-phase2-config-consolidation in third_party/liberator-trading-api,
   then the same branch in the engine repo.

3. Write the implementation plan FIRST at
   docs/plans/liberator-session-self-heal/phase2-config-consolidation.md following the csm-set
   phase1-sample format (frontmatter, TOC, Overview, AI Prompt verbatim, Scope, Design Decisions,
   Implementation Steps, File Changes, Success Criteria). Commit it as its own docs() commit before
   coding.

4. Implementation — submodule:
   4a. Map the current state: the existing session_status.monitoring block (schema A) and
       session_monitor block (schema B), every Pydantic model and every reader.
   4b. Implement a unified SessionMonitorConfig: one enabled, one cadence, all monitor fields once,
       mypy-strict, future annotations.
   4c. Update all consumers of the removed/renamed config paths (no logic change).
   4d. Update example/sample configs to the consolidated schema with inline comments; remove the
       duplicate blocks.
   4e. Tests (PRE-EXISTING-RED warning: ~241 failing tests / ~1279 ruff findings — scope the gate to
       touched files only): parse a unified-schema YAML → correct SessionMonitorConfig; enabled=false
       default; cadence parses; model_dump() round-trip.
   4f. Submodule commit + push (D6) BEFORE touching the engine repo; open a PR.

5. Engine repo: 5a migrate docker/liberator/session_status.yaml to the consolidated schema (monitor
   stays disabled); 5b pin-bump the submodule; 5c mark ROADMAP Phase 2 complete; 5d update the plan
   doc to Complete + Completion Notes; 5e full engine quality gate (ruff/format/mypy/pytest, ≥90%);
   5f engine commit.

6. Knowledge/memory/playbook updates only where the old dual-schema keys are referenced.

7. Open two PRs (liberator-trading-api + quant-execution-engine) with full bodies.

9. Hard constraints: D6 dual-commit (submodule pushed before the engine pin-bump); D5 no
   frozen-contract change; monitor stays disabled (enabled: false); submodule gate scoped to touched
   files; engine gate full; no secrets (placeholders only); no logic changes — config consolidation
   only.
```

> **Convention reconciliation (user direction: "do it in best practice", 2026-06-14).** The prompt's
> central instruction — *"collapse `session_status.monitoring` and `session_monitor` into one
> Pydantic model"* (4a/4b) — rests on a factual misreading of the code (see Design Decisions §1):
> there is no `session_status.monitoring` Pydantic model, and `SessionMonitorConfig` is already the
> single unified schema. The work was therefore implemented as a **config-file consolidation** (drop
> the dead, unread YAML blocks; make every config speak the one consumed schema) plus the
> code-flagged removal of the dead `SessionStatusTestPayload` — which is the best-practice reading of
> "one monitoring schema, clearly documented." The `mypy --strict` / `from __future__ import
> annotations` constraints describe the *engine* repo, not this pre-existing-red submodule (0 of 73
> `app/` modules use future annotations; the repo runs `mypy app/ --ignore-missing-imports`); the
> touched modules keep the surrounding style.

---

## Scope

### In Scope (Phase 2)

| Deliverable | Description | Status |
|---|---|---|
| One consumed schema in all configs | `session_monitor` + `session_status.config`/`.health`; drop dead `monitoring` / `health_check` | Complete |
| Sample-footgun fix | `session_status.sample.yaml.example` gains the (disabled) `session_monitor` it was missing | Complete |
| Dead-model removal | Remove `SessionStatusTestPayload` + `SessionStatusResponse.test_payload_used` + its 6 references | Complete |
| Config-parse test | New `tests/test_models/test_session_status_config.py` (parametrized over both shipped examples) | Complete |
| Mounted config migration | `docker/liberator/session_status.yaml` → consumed schema, monitor disabled | Complete |
| ROADMAP + pin bump | Phase 1 `[x]` + Phase 2 `[x]`; submodule pin → pushed Phase-2 SHA (D6) | Complete |

### Out of Scope (later phases)

- Single-flight lock + exponential backoff + trading-hours respect for re-login (**Phase 3**)
- Fail-loud OTP-timeout alerting (**Phase 4**)
- Enabling the monitor + end-to-end self-heal verification (**Phase 5**)
- Operator runbook / ops docs (**Phase 6**)
- The `login_config` / `api_config` / `defaults` example blocks (orthogonal — not the monitor dup)
- The repo-wide pre-existing test/lint debt (≈241 failing tests, ≈1279 ruff findings) — separate effort
- The orthogonal uncommitted `docker-compose.liberator.yml` host-port edit (Phase 5 concern)

---

## Design Decisions

1. **The premise correction — one model, not two; the dup is at the YAML level.** Verified against
   the code: `app/models/session_status.py` defines exactly **one** monitor model,
   `SessionMonitorConfig`, already with a single `enabled` + single cadence (`checking_time_interval`).
   There is no Pydantic "schema A" for `session_status.monitoring`. Grepping `app/` + `scripts/`:
   - `session_monitor_service.py` reads **only** `config_data.get("session_monitor", {})`.
   - `session_status_service.py` reads **only** `session_status.config` + `session_status.health`.
   - **No code reads** `session_status.monitoring` or `session_status.health_check` — they are dead
     YAML (the global `Settings` in `app/config.py` loads only `system.yaml`).
   So "consolidation" is making the configs speak the consumed schema, not merging Python models.
2. **The one consumed schema.** `session_monitor` → `SessionMonitorConfig`; `session_status.config`
   → `SessionStatusConfig`; `session_status.health` → `SessionStatusHealthConfig`. Every shipped
   config now carries exactly these, with inline comments naming the model each block feeds.
3. **Remove the dead `SessionStatusTestPayload`.** Phase 1 swapped the probe to the read-only
   `GET /api/v1/profile`, leaving `SessionStatusResponse.test_payload_used` always `None` and the
   model annotated "Slated for removal in Phase 2." Best practice is to finish that: removing the
   model + field also deletes the misleading placeholder `accountNo`/PIN the type carried. Blast
   radius is submodule-only — the engine never reads that response field (D5 holds).
4. **Mounted config is explicit.** `docker/liberator/session_status.yaml` is a deterministic operator
   config; it now writes out `session_status.config` + `.health` (the consumed knobs) rather than
   relying on code defaults, so every value the two services read is visible and zero dead keys remain.
5. **Monitor stays disabled.** `session_monitor.enabled: false` + `auto_connect: false` in the
   mounted config and both samples — enabling is Phase 5.
6. **D6 dual-commit + merge-then-pin.** The submodule change landed first (commit + push + PR
   **merged**); the engine then pins the **merged** submodule `main` SHA — never an unpushed SHA.

---

## Implementation Steps

### Submodule (`third_party/liberator-trading-api`)

1. **`config/session_status.sample.yaml.example`** — rewritten to the consumed schema: dropped the
   dead `monitoring` + `health_check`; added the disabled `session_monitor` it was missing; kept
   `session_status.config` + `.health`; documented each block.
2. **`config/session_status.yaml.example`** — dropped the `test_payload` block; the `session_monitor`
   block set to safe disabled defaults (`enabled/auto_connect: false`, cadence 300).
3. **`app/models/session_status.py`** — removed `SessionStatusTestPayload` (and its now-unused
   `Decimal` import) + the `SessionStatusResponse.test_payload_used` field; clarified the
   `SessionMonitorConfig` docstring as the single monitor schema.
4. **`app/services/session_status_service.py`** — dropped the four `test_payload_used=None` kwargs.
5. **`app/api/endpoints/session_status.py`** — dropped the two `"test_payload_used": null` docstring
   examples.
6. **`tests/test_api/test_session_status.py`** — removed the `SessionStatusTestPayload` import + the
   two fixture kwargs; rewrote `test_session_status_with_config_included` to drop the probe-unused
   field (now asserts it is absent from the response).
7. **`tests/test_services/test_session_status_service.py`** — removed the
   `assert response.test_payload_used is None` line.
8. **`tests/test_models/test_session_status_config.py`** (new) — parametrized over both shipped
   examples: monitor parses + ships disabled (cadence 300); `session_status.config`/`.health` parse;
   no dead `monitoring`/`health_check`; `model_dump()` round-trips.

### Engine (`quant-execution-engine`)

9. **`docker/liberator/session_status.yaml`** — dropped the dead `session_status.monitoring` +
   `health_check`; added explicit `session_status.config` + `.health`; kept `session_monitor`
   disabled; refreshed the header comment.
10. **`docs/plans/liberator-session-self-heal/ROADMAP.md`** — Phase 1 `[x]`, Phase 2 `[x]`, Status
    cells filled, top status line updated.
11. **Submodule pin bump** → the merged Phase-2 `main` SHA (D6).

---

## File Changes

### `third_party/liberator-trading-api` (submodule)

| File | Action | Description |
|---|---|---|
| `config/session_status.sample.yaml.example` | REWRITE | Consumed schema; add the missing disabled `session_monitor`; drop dead `monitoring`/`health_check`/`test_payload` |
| `config/session_status.yaml.example` | MODIFY | Drop `test_payload`; monitor set to safe disabled defaults |
| `app/models/session_status.py` | MODIFY | Remove `SessionStatusTestPayload` + `test_payload_used` (+ unused `Decimal`); clarify `SessionMonitorConfig` docstring |
| `app/services/session_status_service.py` | MODIFY | Drop 4× `test_payload_used=None` |
| `app/api/endpoints/session_status.py` | MODIFY | Drop 2× `"test_payload_used": null` doc example |
| `tests/test_api/test_session_status.py` | MODIFY | Drop import + fixture kwargs; rewrite the config-included test |
| `tests/test_services/test_session_status_service.py` | MODIFY | Drop the `test_payload_used` assertion |
| `tests/test_models/test_session_status_config.py` | CREATE | Config-parse / disabled / round-trip / no-dead-block tests |

### `quant-execution-engine` (engine)

| File | Action | Description |
|---|---|---|
| `docker/liberator/session_status.yaml` | MODIFY | Drop dead `monitoring`/`health_check`; explicit `config`/`health`; monitor stays disabled |
| `docs/plans/liberator-session-self-heal/ROADMAP.md` | MODIFY | Phase 1 `[x]` + Phase 2 `[x]` + Status cells |
| `docs/plans/liberator-session-self-heal/phase2-config-consolidation.md` | CREATE | This plan document |
| `third_party/liberator-trading-api` | BUMP | Submodule pin → the merged Phase-2 SHA (D6) |

---

## Success Criteria

- [x] One monitoring config schema; no duplicate `enabled` flags; the dead
      `session_status.monitoring` / `health_check` blocks are gone from every shipped config.
- [x] `session_status.sample.yaml.example` (the `setup.py` source) carries the disabled
      `session_monitor` (footgun fixed).
- [x] `SessionStatusTestPayload` + `test_payload_used` removed; no placeholder `accountNo`/PIN
      remains in the probe path or configs.
- [x] Monitor stays disabled — `session_monitor.enabled: false` + `auto_connect: false` everywhere.
- [x] No engine `src/` change; no frozen `NormalizedOrder` / state machine / capability cells /
      gating / infra-db change (D5).
- [x] Submodule scoped gate: ruff no new findings, mypy clean, the touched tests pass.
- [x] Engine full gate green (ruff / format / mypy / pytest, ≥90%); coverage unchanged.
- [x] Dual-commit + pin bump (D6) — submodule merged, then the engine pins the merged SHA.

---

## Completion Notes

### Summary

Implemented as the corrected config-level consolidation. `SessionMonitorConfig` was already the
single monitor model; the duplication lived only in YAML. All three shipped configs now speak the
one consumed schema (`session_monitor` + `session_status.config`/`.health`), the dead
`session_status.monitoring` / `health_check` blocks are gone, and the dead `SessionStatusTestPayload`
model + `test_payload_used` field (always `None` since Phase 1) were removed. The monitor remains
disabled. The submodule landed first (liberator-trading-api **PR #39, merged → `e82c694`**) and the
engine pins that merged SHA (D6).

### Issues encountered / findings

1. **The prompt's "merge two Pydantic schemas" premise was inaccurate.** There is no
   `session_status.monitoring` Pydantic model; `SessionMonitorConfig` already had one `enabled` + one
   cadence. The real fix was dropping the dead YAML + aligning every config — surfaced to the operator,
   who chose "do it in best practice."
2. **Latent footgun in the setup.py provisioning sample.** `config/session_status.sample.yaml.example`
   (mapped by `setup.py:41` to the generated `session_status.yaml`) was **missing `session_monitor`
   entirely** — a generated config would have left the monitor at `SessionMonitorConfig`'s default
   `enabled=True`. The bundled engine deployment is safe (its mounted overlay sets `enabled: false`),
   but the sample now carries the disabled block.
3. **The three configs had three different shapes.** The mounted overlay used `monitoring` /
   `health_check` (dead), the doc example used `config` / `health` + `session_monitor`, and the setup
   sample used `test_payload` / `monitoring` / `health_check` (with no `session_monitor`). All now match.
4. **Pre-existing-red submodule — scoped gate.** Per plan: ruff introduced zero new findings (model
   29 → 28; the other four touched files unchanged vs `main`; the new test file is clean), mypy clean
   on the touched modules, 46 touched tests pass. The repo-wide debt is out of scope.
5. **The pin advances past the Phase-1 merge.** Engine `main` recorded the submodule at the Phase-1
   *branch* tip (`59eb76a`); it now advances to the merged Phase-2 `main` tip (`e82c694`) — a clean
   fast-forward (the content delta is exactly the Phase-2 changes, since Phase-1's content was already
   pinned).

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Opus 4.8)
**Status:** Complete
**Completed:** 2026-06-14

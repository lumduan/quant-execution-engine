# Phase 6: Docs / runbook (the final phase)

**Feature:** Liberator session self-heal — Phase 6: Operator docs + runbook
**Branch:** `feat/liberator-session-self-heal-phase6-docs-runbook`
**Created:** 2026-06-14
**Status:** Complete
**Completed:** 2026-06-14
**Depends On:** [`phase5-enable-monitor.md`](phase5-enable-monitor.md) (Complete); [`ROADMAP.md`](ROADMAP.md) Phase 6

---

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [File Changes](#file-changes)
6. [Success Criteria](#success-criteria)
7. [Completion Notes](#completion-notes)

---

## Overview

### Purpose

Phases 1–5 built, hardened, **enabled**, and **live-verified** the auto-relogin monitor. Phase 6 — the
final phase — **documents it for operators**, because the feature does real things: it auto-logs-in a
real broker session and **fires real OTP SMS**, and its unattended success depends on the operator's
always-on iPhone OTP-forward automation (no refresh token). Operators must understand: what the monitor
does, the **iPhone-automation dependency**, the **fail-loud alert response** (`session.relogin_otp_timeout`
→ check the phone), how to enable / disable it, and the two live gotchas Phase 5 surfaced (pin-bump ≠
image redeploy; trading-hours config is Redis-cached).

**Docs-only — no code, no config, no submodule / pin behaviour change** (other than the umbrella pin
bump to the merged docs SHA).

### Parent Plan Reference

- [`docs/plans/liberator-session-self-heal/ROADMAP.md`](ROADMAP.md) — feature roadmap (Phase 6 is the last)
- Engine ROADMAP: [`docs/plans/ROADMAP.md`](../ROADMAP.md)
- Order-routing safety playbook: [`../../../.claude/playbooks/order-routing-safety.md`](../../../.claude/playbooks/order-routing-safety.md)

### Key Deliverables

1. **Reference doc** — `docs/operations/liberator-session-self-heal.md` (the single source of substance).
2. **Pointers** — the hub, troubleshooting, the safety playbook, `CLAUDE.md`, and the umbrella runbook
   all point to the reference doc (link, don't duplicate).
3. **ROADMAP** — Phase 6 `[x]`; the feature marked **complete**.

---

## AI Prompt

Started by **"start phase 6"** (after **"compact context before do this phase"**). The spec is the
ROADMAP's Phase 6 deliverables (operator runbook, ops docs, a note in the engine `CLAUDE.md`).

```
Document the now-enabled Liberator session self-heal for operators. A focused, operator-first set:
one reference doc (docs/operations/liberator-session-self-heal.md) holding the substance — the
self-heal + fail-loud behavior, the always-on iPhone-automation dependency, the full config surface
(session_monitor.* YAML + RELOGIN_* / OTP_AUTO_CONFIRM_ENABLED env), enable/disable, the two live
gotchas, and the verified Phase-5 log evidence — with the playbooks / hub / CLAUDE.md / troubleshooting
pointing to it. Docs-only; accurate, not aspirational (single-flight is unit-proven; engine-breaker
recovery is adapter-mode/sim here). No secrets. Two repos: engine PR merged, then the umbrella runbook +
pin bump.
```

---

## Scope

### In Scope (Phase 6)

| Deliverable | Description | Status |
|---|---|---|
| Reference doc (NEW) | `docs/operations/liberator-session-self-heal.md` — the full operator reference | Complete |
| Hub link | Add the ref doc to `docs/README.md` operations table | Complete |
| Troubleshooting entry | `session.relogin_otp_timeout` → check the phone + the rebuild gotcha | Complete |
| Safety-playbook subsection | "Session self-heal" under **Liberator specifics** | Complete |
| Engine `CLAUDE.md` | One-line "monitor enabled" note + ops-table row | Complete |
| ROADMAP | Phase 6 `[x]`; feature complete | Complete |
| Phase plan doc (NEW) | This file | Complete |
| Umbrella runbook | "Liberator session self-heal" section in `execution-engine-runbook.md` | Complete |
| Umbrella pin bump | Bump `quant-execution-engine` to the merged Phase-6 SHA | Complete |

### Out of Scope

- Any code / config / behaviour change (the monitor was enabled + verified in Phase 5).
- Any submodule change (the hardened monitor is pinned at `911255a`).

---

## Design Decisions

1. **One reference doc; everything else points to it.** The substance lives in
   `docs/operations/liberator-session-self-heal.md`; the hub, troubleshooting, both playbooks, and
   `CLAUDE.md` link to it. Avoids the drift of duplicated runbooks.
2. **Accurate, not aspirational.** Document only what Phase 5 verified live. Single-flight is marked
   **unit-proven** (no forced live race); engine-breaker recovery is marked **adapter-mode** (the
   Phase-5 engine ran in sim, so its breaker recovery is the behaviour that applies at `paper` /
   `micro_live`, not something the sim run exercised).
3. **Operator-first.** Lead with the loop, the iPhone dependency, and the fail-loud response — the three
   things an operator must hold in their head — then the config surface and gotchas.
4. **Two repos, merge-then-pin.** The engine PR merges first; the umbrella then adds its runbook section
   and bumps the engine pin to the merged SHA (never pin against an unmerged SHA).

---

## File Changes

### Engine repo (`quant-execution-engine`)

| File | Action | Description |
|---|---|---|
| `docs/operations/liberator-session-self-heal.md` | CREATE | The operator reference (behavior, iPhone dependency, fail-loud + response, config surface, two gotchas, Phase-5 evidence) |
| `docs/README.md` | MODIFY | Add the ref doc to the operations table |
| `docs/operations/troubleshooting.md` | MODIFY | New `session.relogin_otp_timeout` entry + the rebuild-after-pin note |
| `.claude/playbooks/order-routing-safety.md` | MODIFY | New "Session self-heal (auto-relogin)" subsection under **Liberator specifics** |
| `CLAUDE.md` | MODIFY | Monitor-enabled note + ops-table row |
| `docs/plans/liberator-session-self-heal/ROADMAP.md` | MODIFY | Phase 6 `[x]`; success criteria; feature complete |
| `docs/plans/liberator-session-self-heal/phase6-docs-runbook.md` | CREATE | This plan doc |

### Umbrella repo (`quant-trading-system`)

| File | Action | Description |
|---|---|---|
| `.claude/playbooks/execution-engine-runbook.md` | MODIFY | New "Liberator session self-heal" section (cross-repo operator view) |
| `quant-execution-engine` (pin) | MODIFY | Bump to the merged Phase-6 engine SHA |

---

## Success Criteria

- [x] A dedicated reference doc documents the self-heal + fail-loud behavior, the iPhone-automation
      dependency, the full config surface, enable/disable, and the two live gotchas — accurately
      (single-flight unit-proven; engine-breaker recovery adapter-mode).
- [x] The fail-loud alert's **operator response** (check the iPhone automation) is unambiguous.
- [x] The ref doc is reachable from the hub, `CLAUDE.md`, the safety playbook, troubleshooting, and the
      umbrella runbook; relative paths resolve across both repos.
- [x] No secrets in any example (placeholders only).
- [x] Docs-only; the engine quality gate stays green; CI 3.11 + 3.12 green.
- [x] Two-repo merge-then-pin: engine PR merged, then the umbrella runbook + pin bump.

---

## Completion Notes

### Summary

The Liberator session self-heal is fully documented for operators: a single reference doc carries the
substance, with the hub / troubleshooting / safety playbook / `CLAUDE.md` / umbrella runbook pointing to
it. Docs-only — no code, config, or submodule behaviour change. **This is the last phase; the feature is
complete.**

### Notes

- The fail-loud env vars (`RELOGIN_OTP_WAIT_SECONDS` / `RELOGIN_OTP_POLL_SECONDS` /
  `RELOGIN_NOTIFY_WEBHOOK_URL`) and `OTP_AUTO_CONFIRM_ENABLED` are the **bundled liberator app's own**
  settings (no `EXECUTION_ENGINE_` prefix) — documented as such so they are not confused with the
  engine-side `EXECUTION_ENGINE_LIBERATOR_*`.
- The two gotchas are documented as first-class operator knowledge because both bit the Phase-5 live run
  (a stale image ran old code; the trading-hours override needed a Redis cache clear).

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Opus 4.8)
**Status:** Complete
**Completed:** 2026-06-14

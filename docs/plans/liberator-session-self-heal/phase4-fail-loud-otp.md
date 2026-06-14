# Phase 4: Fail-loud OTP Fallback + Alerting

**Feature:** Liberator session self-heal — Phase 4: Fail-loud OTP fallback + alerting
**Branch:** `feat/liberator-session-self-heal-phase4-fail-loud-otp`
**Created:** 2026-06-14
**Status:** Complete
**Completed:** 2026-06-14
**Depends On:** [`phase3-relogin-hardening.md`](phase3-relogin-hardening.md) (Complete); [`ROADMAP.md`](ROADMAP.md) Phase 4; decisions **D4**, **D5**, **D6**

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

The Liberator session has **no refresh token**, so every re-login fires a **fresh OTP SMS** that only
closes the loop if the operator's always-on iPhone automation forwards it to the webhook. **If the
phone is off, the re-login can never complete** — and the monitor failed *silently*: `login()`
returns `success=True` once the broker **accepts the login request** (which only **triggers the OTP
SMS**); confirmation is asynchronous (iPhone → `POST /api/v1/otp/sms` → `confirm_otp` → a fresh token
is stored). The monitor treated a triggered login as **"session restored"**.

Phase 4 makes a dead phone **LOUD** (decision **D4**): after a triggered re-login, watch for the OTP
to actually confirm within a timeout; on timeout, emit a structured alert and **keep the session
marked dead** (engine breaker stays open) — never a false "alive". **The monitor stays DISABLED**
(Phase 5 enables it + does the real fail-loud E2E).

### Parent Plan Reference

- [`docs/plans/liberator-session-self-heal/ROADMAP.md`](ROADMAP.md) — feature roadmap (D4, D5, D6)
- Engine ROADMAP: [`docs/plans/ROADMAP.md`](../ROADMAP.md)
- Cross-cutting: [`plans/feature-execution-engine/ROADMAP.md`](../../../../plans/feature-execution-engine/ROADMAP.md)

### Key Deliverables

1. **"Awaiting OTP" tracking** — after a triggered login, watch for confirmation (a fresh token).
2. **Pluggable notifier** — NEW `app/services/notify_service.py` (structured log + optional webhook).
3. **Stay-dead on timeout** — keep the session dead so the engine breaker stays open.
4. **Settings** — `app/config.py`: OTP-wait timeout, poll interval, notifier target.
5. **Unit tests** — OTP-confirmed → restored; OTP-timeout → alert fired + stays dead.

---

## AI Prompt

This phase was started by the user instruction **"start Phase 4 fail-loud OTP"**; the spec is the
ROADMAP's Phase 4 section. Objective (verbatim intent):

```
Implement Phase 4 — Fail-loud OTP fallback + alerting of the Liberator session self-heal feature.
After a triggered re-login (login() only fires the OTP SMS; confirmation is async via the webhook),
watch for the OTP to actually confirm within a timeout. On timeout, emit a structured alert (+ an
optional webhook) and keep the session marked dead so the engine breaker stays open — never a false
"alive". Reuse OTPIntegrationService result / current_attempt_counts / token refresh. Add a pluggable
notifier (NEW app/services/notify_service.py) and settings (app/config.py: OTP-wait timeout +
notifier target). Unit tests: OTP-confirmed-in-time -> alive; OTP-timeout -> alert + stays dead.
Submodule-first dual-commit (D6); monitor stays DISABLED (Phase 5 enables it); no engine src / frozen
contract / infra-db change (D5); scope the pre-existing-red submodule gate to touched files.
```

> **Convention reconciliation (user direction: "do it in best practice").** The "awaiting OTP"
> mechanism is implemented as **token-refresh detection** (poll `get_current_auth_tokens().timestamp`
> against a pre-login baseline) — the most direct "was the OTP confirmed (a fresh token stored)"
> signal, with no broker round-trip, reusing the existing auth-token service. New code uses modern
> typing so the touched files stay ruff-clean; the file's pre-existing `Optional[...]`/format debt is
> left untouched (no global pass).

---

## Scope

### In Scope (Phase 4)

| Deliverable | Description | Status |
|---|---|---|
| "Awaiting OTP" tracking | `_await_otp_confirmation` polls for a token newer than the pre-login baseline within `relogin_otp_wait_seconds` | Complete |
| Pluggable notifier | NEW `app/services/notify_service.py` — structured `session.relogin_otp_timeout` log + optional best-effort webhook | Complete |
| Stay-dead on timeout | on timeout: alert, `_consecutive_failures += 1`, no `_total_reconnects`, **no re-trigger**; session stays dead | Complete |
| Settings | `relogin_otp_wait_seconds` / `relogin_otp_poll_seconds` / `relogin_notify_webhook_url` (env-backed) | Complete |
| Unit tests | confirmed → restored; timeout → alert + stays dead (exactly one OTP); notifier suite | Complete |

### Out of Scope (later phases)

- Enabling the monitor + the real fail-loud **E2E** (phone unreachable → alert) (**Phase 5**)
- Operator runbook / ops docs (**Phase 6**)
- The repo-wide pre-existing test/lint debt (≈241 failing tests / ≈1279 ruff findings)

---

## Design Decisions

1. **The bug: a triggered login ≠ a restored session.** `login()` returns `success` on OTP *trigger*;
   the session is restored only once the OTP is confirmed and a **fresh token** is stored
   (`store_auth_tokens` stamps `time.time()`). The monitor's success branch is rewired to await that.
2. **Token-refresh detection (over a liveness probe).** `_await_otp_confirmation(baseline)` polls
   `redis_auth_token_service.get_current_auth_tokens()` for `timestamp > baseline` within
   `relogin_otp_wait_seconds` (deadline via `time.monotonic`, interval via `asyncio.sleep`). Directly
   answers "did the OTP confirm" with no broker round-trip; the monitor's next poll re-probes liveness.
3. **Fail loud, stay dead, don't re-trigger.** On timeout: `_notify_relogin_otp_timeout` fires and the
   session is kept dead (`_consecutive_failures += 1`, **no** `_total_reconnects`) so the engine
   breaker stays open. The reconnection retry loop still only retries a **trigger failure**, never an
   OTP timeout (another login = another OTP — burning OTPs is worse than a loud alert).
4. **Pluggable notifier, best-effort.** `NotifyService.notify` always emits a **structured log** (the
   alert of record) and, when `relogin_notify_webhook_url` is set, a **best-effort webhook POST**
   (never raises; **no secrets** in the payload).
5. **Env-backed settings.** The three knobs live in `app/config.py::Settings` (the monitor already
   holds `self._settings`); operators set `RELOGIN_*` in the liberator `.env`. The mounted
   `session_status.yaml` is unchanged.
6. **Monitor stays DISABLED; D5/D6 hold.** No engine `src/` change, no frozen-contract / infra-db
   change; submodule-first dual-commit then engine pin.

---

## Implementation Steps

### Submodule `third_party/liberator-trading-api`
1. **`app/config.py`** — add `relogin_otp_wait_seconds` (300), `relogin_otp_poll_seconds` (5),
   `relogin_notify_webhook_url` ("").
2. **`app/services/notify_service.py`** (NEW) — `NotifyService.notify` + a module-global accessor.
3. **`app/services/session_monitor_service.py`** — `_capture_token_baseline`,
   `_await_otp_confirmation`, `_notify_relogin_otp_timeout`; rewire the success branch of
   `_attempt_immediate_login` + `_attempt_reconnection` to await-confirm-or-fail-loud.
4. **Tests** — stub the OTP-await in the success/failure tests; new OTP-timeout / `_await_otp` tests +
   `tests/test_services/test_notify_service.py`.

### Engine `quant-execution-engine`
5. **`ROADMAP.md`** — Phase 4 `[x]` + status cells + the "missing-OTP alert" success criterion.
6. **Submodule pin bump** → the merged Phase-4 `main` SHA (D6).

New operator env vars (documented here, set in the liberator `.env`; defaults are safe):
`RELOGIN_OTP_WAIT_SECONDS`, `RELOGIN_OTP_POLL_SECONDS`, `RELOGIN_NOTIFY_WEBHOOK_URL`.

---

## File Changes

### `third_party/liberator-trading-api` (submodule)

| File | Action | Description |
|---|---|---|
| `app/config.py` | MODIFY | add the three `relogin_*` settings |
| `app/services/notify_service.py` | CREATE | structured-log + optional best-effort webhook notifier |
| `app/services/session_monitor_service.py` | MODIFY | await-OTP-confirm + fail-loud in both re-login success branches |
| `tests/test_services/test_session_monitor_service.py` | MODIFY | stub OTP-await; new timeout / await tests |
| `tests/test_services/test_notify_service.py` | CREATE | notifier log / webhook / swallow-on-failure |

### `quant-execution-engine` (engine)

| File | Action | Description |
|---|---|---|
| `docs/plans/liberator-session-self-heal/ROADMAP.md` | MODIFY | Phase 4 `[x]` + the missing-OTP-alert success criterion |
| `docs/plans/liberator-session-self-heal/phase4-fail-loud-otp.md` | CREATE | This plan document |
| `third_party/liberator-trading-api` | BUMP | Submodule pin → the merged Phase-4 SHA (D6) |

> The mounted `docker/liberator/session_status.yaml` is **unchanged** — the new knobs are env-backed.

---

## Success Criteria

- [x] After a triggered re-login, the monitor waits for the OTP to actually confirm (a fresh token).
- [x] A missing OTP (phone off) → a structured `session.relogin_otp_timeout` alert and the session
      stays dead (engine breaker stays open) — never a false "alive".
- [x] Exactly one OTP per dead-session event — an OTP timeout does **not** re-trigger another login.
- [x] Optional best-effort webhook (no secrets); a webhook failure never breaks the re-login path.
- [x] Monitor stays DISABLED (`enabled: false` / `auto_connect: false`).
- [x] No engine `src/` change; no frozen `NormalizedOrder` / state machine / capability / gating /
      infra-db change (D5).
- [x] Scoped submodule gate: ruff zero new findings, mypy clean; new tests pass; no regression (the 2
      pre-existing monitor-test failures unchanged).
- [x] Dual-commit + pin bump (D6) — submodule merged, then the engine pins the merged SHA.

---

## Completion Notes

### Summary

Implemented as designed, entirely in the submodule's `SessionMonitorService` plus a new
`NotifyService` and three settings. The re-login success branch now **awaits OTP confirmation**
(a fresh token newer than the pre-login baseline) within `relogin_otp_wait_seconds`; on timeout it
**fails loud** (`session.relogin_otp_timeout` alert) and keeps the session dead — never resetting
counters or claiming a reconnect, and never re-triggering another OTP. The monitor remains disabled.
Submodule landed first (liberator-trading-api **PR #41, merged → `911255a`**); the engine pins that
SHA (D6).

### Issues encountered / findings

1. **`login()` only triggers the OTP.** Confirmed against `login_service.login()` — `success=True`
   means the broker accepted the login request (OTP SMS sent), not that the session is alive. The
   fresh token from the async `confirm_otp` is the real "restored" signal.
2. **The success tests had to be re-stubbed.** With the new await, `test_attempt_reconnection_success`
   would otherwise poll a real Redis for the full `relogin_otp_wait_seconds` — the success/failure
   tests now stub `_await_otp_confirmation` / `_capture_token_baseline` (and the dedicated
   `_await_otp_confirmation` tests mock the token service + `time.monotonic` + `asyncio.sleep`).
3. **Two pre-existing failures remain** (`test_health_check_degraded`, `test_configuration_validation`)
   — unchanged from the Phase-3 baseline, out of scope. Monitor test file: 27 passed (+4) / 2 failed;
   the new `NotifyService` suite: 3 passed.
4. **New code uses modern typing** (`dict[str, Any] | None`, `from datetime import UTC`) so the touched
   files keep a ruff delta of 0 vs `main`; the engine repo is untouched (no `src/` change).

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Opus 4.8)
**Status:** Complete
**Completed:** 2026-06-14

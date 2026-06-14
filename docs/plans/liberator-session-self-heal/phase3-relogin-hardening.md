# Phase 3: Re-login Hardening

**Feature:** Liberator session self-heal — Phase 3: Re-login hardening
**Branch:** `feat/liberator-session-self-heal-phase3-relogin-hardening`
**Created:** 2026-06-14
**Status:** Complete
**Completed:** 2026-06-14
**Depends On:** [`phase2-config-consolidation.md`](phase2-config-consolidation.md) (Complete); [`ROADMAP.md`](ROADMAP.md) Phase 3; decisions **D3**, **§C**, **D5**, **D6**

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

The bundled `SessionMonitorService` can auto-relogin a dead Liberator session, but its re-login
path was **unsafe to enable**: fixed (non-backed-off) retries, **no single-flight lock** (the
monitor poll, a manual trigger, and a future engine nudge could each fire a login → multiple OTP
SMS), no trading-hours gate inside the re-login path (could burn OTPs after market close), and a
stale `_should_trigger_immediate_login` that no longer matched Phase-1's read-only probe output.

Phase 3 makes the re-login **safe, non-racing, and backed-off** — reusing the existing loop/methods,
not rewriting. **The monitor stays DISABLED** (`enabled: false` / `auto_connect: false`); turning it
on + the live OTP end-to-end is **Phase 5**.

### Parent Plan Reference

- [`docs/plans/liberator-session-self-heal/ROADMAP.md`](ROADMAP.md) — feature roadmap (D3, §C, D5, D6)
- Engine ROADMAP: [`docs/plans/ROADMAP.md`](../ROADMAP.md)
- Cross-cutting: [`plans/feature-execution-engine/ROADMAP.md`](../../../../plans/feature-execution-engine/ROADMAP.md)

### Key Deliverables

1. **Single-flight re-login lock (D3)** — Redis `SETNX`(+TTL) around the re-login critical section.
2. **Exponential backoff + jitter** — replace the fixed `retry_backoff_seconds` sleep.
3. **Trading-hours respect (§C)** — gate re-login on the existing `_check_trading_hours_automation`.
4. **Fix `_should_trigger_immediate_login`** — match Phase-1's probe vocabulary (the ROADMAP crux).
5. **Unit tests** — single-flight, backoff schedule, trading-hours pause, immediate-login signals.

---

## AI Prompt

This phase was started by the user instruction **"start Phase 3 re-login hardening"**; the spec is
the ROADMAP's Phase 3 section. Objective (verbatim intent):

```
Implement Phase 3 — Re-login hardening of the Liberator session self-heal feature. Harden the
bundled SessionMonitorService's re-login path IN PLACE (reuse _monitor_loop / _check_and_reconnect /
_attempt_immediate_login / _attempt_reconnection / _check_trading_hours_automation / liberator-redis
— do not rewrite):
  - Single-flight lock (D3): a Redis SETNX lock (liberator-redis) around the re-login critical
    section, with a TTL so a crashed login self-clears — only one login / one OTP at a time.
  - Exponential backoff + jitter: replace the fixed retry_backoff_seconds with capped exponential
    + jitter.
  - Trading-hours respect (§C): re-login attempts honour _check_trading_hours_automation so OTPs
    aren't fired when markets are closed.
  - Unit tests: concurrent triggers → one login; backoff schedule; trading-hours pause.
Submodule-first dual-commit (D6); monitor stays DISABLED (Phase 5 enables it); no engine src / frozen
contract / infra-db change (D5); scope the pre-existing-red submodule gate to touched files.
```

> **Convention reconciliation (user direction: "do it in best practice").** Two best-practice
> extensions beyond the literal list, both verified necessary: (1) **the sync-redis bug** —
> `session_monitor_service.py` was the only service using sync `import redis` while `await`-ing
> `.ping()`/`.get()`; the SETNX lock cannot work until it uses `redis.asyncio` like every other
> service, so that one-line fix is included. (2) **`_should_trigger_immediate_login`** — flagged as a
> Phase-1 crux (it matched the dead pre-place probe text); updated to Phase-1's structured signals.
> New code uses modern typing (`str | None`, `collections.abc.Awaitable`) to keep the touched files
> ruff-clean; the file's pre-existing `Optional[...]`/format debt is left untouched (no global pass).

---

## Scope

### In Scope (Phase 3)

| Deliverable | Description | Status |
|---|---|---|
| Single-flight lock (D3) | Redis `SETNX`+TTL around `_attempt_immediate_login` / `_attempt_reconnection`; token-guarded release; fail-open on Redis outage | Complete |
| Exponential backoff + jitter | `_compute_backoff` (capped exp + equal jitter) + `retry_backoff_max_seconds` config field | Complete |
| Trading-hours respect (§C) | `_relogin_allowed_now` gate before each attempt (incl. mid-loop) | Complete |
| Fix `_should_trigger_immediate_login` | structured `status`/`error_details` match of Phase-1's probe | Complete |
| sync→async redis | `import redis.asyncio as redis` (prerequisite for the lock) | Complete |
| Unit tests | single-flight / backoff / trading-hours / immediate-login + fixed scaffolding | Complete |

### Out of Scope (later phases)

- Enabling the monitor + end-to-end self-heal verification (**Phase 5**)
- Fail-loud OTP-timeout alerting (**Phase 4**)
- Gating the public `/api/v1/login` endpoint with the same lock (the engine nudge, **§A**, deferred)
- The repo-wide pre-existing test/lint debt (≈241 failing tests / ≈1279 ruff findings)

---

## Design Decisions

1. **Single-flight via Redis SETNX (D3), not an in-process lock.** The monitor poll, a manual
   trigger, and a future cross-process engine nudge can all race; `SET key token NX EX=ttl` on
   liberator-redis gives cross-process mutual exclusion. Release is a **token-guarded Lua
   compare-delete** (never deletes a foreigner's lock); the **TTL** (≈ a full login + OTP wait, 360 s)
   self-clears a crashed login. **Redis-down fails open** (proceed without the lock) so an outage
   never blocks recovery.
2. **Capped exponential with equal jitter.** `_compute_backoff(n) = half + U(0, half)` where
   `half = min(base·2^(n-1), cap)/2` — bounded to `[exp/2, exp]`, guaranteeing a meaningful minimum
   gap between OTP-triggering attempts (equal jitter beats full jitter for OTP-cost). `cap` is the new
   `retry_backoff_max_seconds` (default 300).
3. **Trading-hours gate reuses the existing machinery.** `_relogin_allowed_now` calls the existing
   `_check_trading_hours_automation` (which maintains `_is_trading_paused`) and is checked **before
   each attempt** (incl. mid-loop, since a retry can span market close). Fails open (allowed) when
   trading status is indeterminate — matching the monitor loop's existing behaviour. A skip is **not**
   counted as a failure.
4. **`_should_trigger_immediate_login` keys off structured Phase-1 signals.** Status
   `authentication_error` (no token) or `error_details` containing `auth_token_rejected` (401/403) →
   immediate login; transient `probe_network_error`/`probe_http_error` fall through to the backed-off
   retry path. The stale pre-place/`HTTP Error` strings are removed.
5. **The redis client must be async.** The SETNX lock and the existing trading-hours read both need a
   real async client; the monitor's lone sync `import redis` (awaiting sync returns) was a latent bug,
   fixed to `redis.asyncio` to match the other six redis services.
6. **Monitor stays DISABLED; D5/D6 hold.** No engine `src/` change, no frozen-contract / infra-db
   change; submodule-first dual-commit then engine pin.

---

## Implementation Steps

### Submodule `third_party/liberator-trading-api`

1. **`app/services/session_monitor_service.py`** — `import redis.asyncio as redis` (+ `random`,
   `uuid`, `collections.abc.Awaitable`, `typing.cast`); lock constants (`RELOGIN_LOCK_KEY`, TTL,
   fail-open sentinel, release Lua); new `_compute_backoff`, `_acquire_relogin_lock`,
   `_release_relogin_lock`, `_relogin_allowed_now`; wrap + guard `_attempt_immediate_login` and
   `_attempt_reconnection` (trading-hours → creds → lock → try/finally release; exponential backoff
   in the retry loop); rewrite `_should_trigger_immediate_login`; thread `retry_backoff_max_seconds`
   through `_load_default_config` + `_save_configuration_to_yaml`.
2. **`app/models/session_status.py`** — add `retry_backoff_max_seconds` to `SessionMonitorConfig`
   (default 300 + validator 10..3600).
3. **`config/session_status*.yaml.example`** — surface `retry_backoff_max_seconds` (commented).
4. **`tests/test_services/test_session_monitor_service.py`** — fix `mock_settings` (`liberator_pin`),
   mock `asyncio.sleep`, add the single-flight / backoff / trading-hours / immediate-login tests.

### Engine `quant-execution-engine`

5. **`docker/liberator/session_status.yaml`** — add `retry_backoff_max_seconds: 300` (monitor stays
   `enabled: false` / `auto_connect: false`).
6. **`ROADMAP.md`** — Phase 3 `[x]` + status cells.
7. **Submodule pin bump** → the merged Phase-3 `main` SHA (D6).

---

## File Changes

### `third_party/liberator-trading-api` (submodule)

| File | Action | Description |
|---|---|---|
| `app/services/session_monitor_service.py` | MODIFY | redis.asyncio; lock + backoff + trading-hours guards; `_should_trigger_immediate_login` fix; config wiring |
| `app/models/session_status.py` | MODIFY | add `retry_backoff_max_seconds` (+ validator) |
| `config/session_status.yaml.example`, `config/session_status.sample.yaml.example` | MODIFY | surface the new field (commented) |
| `tests/test_services/test_session_monitor_service.py` | MODIFY | fix scaffolding + new Phase-3 tests |

### `quant-execution-engine` (engine)

| File | Action | Description |
|---|---|---|
| `docker/liberator/session_status.yaml` | MODIFY | add `retry_backoff_max_seconds`; monitor stays disabled |
| `docs/plans/liberator-session-self-heal/ROADMAP.md` | MODIFY | Phase 3 `[x]` |
| `docs/plans/liberator-session-self-heal/phase3-relogin-hardening.md` | CREATE | This plan document |
| `third_party/liberator-trading-api` | BUMP | Submodule pin → the merged Phase-3 SHA (D6) |

---

## Success Criteria

- [x] Only one re-login / one OTP fires per dead-session event, even under concurrent triggers (D3).
- [x] Re-login backs off with capped exponential + jitter (no OTP storm).
- [x] No OTP is fired outside trading hours (§C), incl. when markets close mid-retry.
- [x] `_should_trigger_immediate_login` recognises Phase-1's `auth_token_rejected` /
      `authentication_error` and ignores transient probe errors.
- [x] Monitor stays DISABLED (`enabled: false` / `auto_connect: false`).
- [x] No engine `src/` change; no frozen `NormalizedOrder` / state machine / capability / gating /
      infra-db change (D5).
- [x] Scoped submodule gate: ruff zero new findings, mypy clean; the new tests pass; the monitor test
      file improved 3→2 pre-existing failures (no regression).
- [x] Dual-commit + pin bump (D6) — submodule merged, then the engine pins the merged SHA.

---

## Completion Notes

### Summary

Implemented as designed, entirely in the submodule's `SessionMonitorService` plus one additive
config field. The re-login critical section is now single-flight (Redis `SETNX`+TTL, token-guarded
release, fail-open on outage), backed off with capped exponential + equal jitter, and gated on
trading hours before every attempt; `_should_trigger_immediate_login` now matches Phase-1's probe
signals; and the monitor's lone sync `import redis` (a latent bug — awaiting a sync client) was fixed
to `redis.asyncio`. The monitor remains disabled. Submodule landed first
(liberator-trading-api **PR #40, merged → `6b48a6e`**); the engine pins that SHA (D6).

### Issues encountered / findings

1. **Latent sync-redis bug.** `session_monitor_service.py` was the only service using sync
   `import redis` while `await`-ing `.ping()`/`.get()` — never hit because the monitor is disabled.
   Fixed to `redis.asyncio` (prerequisite for the SETNX lock).
2. **`test_attempt_reconnection_success` was red at baseline.** The `mock_settings` fixture omitted
   `liberator_pin`, so `LoginRequest(pin=<MagicMock>)` raised a ValidationError and the login never
   "succeeded." Adding `liberator_pin` fixed it (3→2 failures on the file).
3. **The reconnection tests really slept.** Baseline ran in **2m5s** because the retry loop's
   `asyncio.sleep(retry_backoff_seconds)` wasn't mocked; the Phase-3 tests mock it (and the backoff is
   now a testable `_compute_backoff`).
4. **Two pre-existing failures left.** `test_health_check_degraded` (untouched health path) and
   `test_configuration_validation` (asserts `checking_time_interval` "at least 30 seconds" while the
   model has always enforced "at least 2") fail identically at baseline and are out of scope.
5. **mypy + ruff on the async redis.** `redis.asyncio`'s `eval` has a union return type; the
   token-guarded release uses `cast("Awaitable[Any]", …)` to stay mypy-clean. New code uses modern
   typing so the touched files keep a ruff delta of 0 vs `main`.

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Opus 4.8)
**Status:** Complete
**Completed:** 2026-06-14

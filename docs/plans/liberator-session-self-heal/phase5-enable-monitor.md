# Phase 5: Enable the Monitor + End-to-End Verification

**Feature:** Liberator session self-heal — Phase 5: Enable in the bundled deployment + E2E
**Branch:** `feat/liberator-session-self-heal-phase5-enable-monitor`
**Created:** 2026-06-14
**Status:** Complete (enablement committed; live self-heal + fail-loud verified)
**Completed:** 2026-06-14
**Depends On:** [`phase4-fail-loud-otp.md`](phase4-fail-loud-otp.md) (Complete); [`ROADMAP.md`](ROADMAP.md) Phase 5

---

## Table of Contents

1. [Overview](#overview)
2. [AI Prompt](#ai-prompt)
3. [Scope](#scope)
4. [Design Decisions](#design-decisions)
5. [Live E2E Evidence](#live-e2e-evidence)
6. [File Changes](#file-changes)
7. [Success Criteria](#success-criteria)
8. [Completion Notes](#completion-notes)

---

## Overview

### Purpose

Phases 1–4 built the hardened auto-relogin machinery (read-only probe, one config schema,
single-flight + backoff + trading-hours, fail-loud OTP) but shipped it **DISABLED**. Phase 5 turns it
**ON** in the bundled deployment (`session_monitor.enabled: true` + `auto_connect: true` in the
engine's mounted `docker/liberator/session_status.yaml`) and **proves the full loop live**. Per the
operator's choice, the approach was **verify-then-commit**: flip locally, prove the self-heal + the
fail-loud loops on the running stack (real OTP, the operator's iPhone forwards it), and commit the
enablement **only after** the loops passed.

This is an **engine-config-only** change — no submodule change, no pin bump (the hardened monitor is
already pinned at `911255a`, Phase 4). The compose `8210:8200` port-publish (uncommitted since
2026-06-13, so the OTP webhook is reachable via Cloudflare) lands here too, per the ROADMAP.

### Parent Plan Reference

- [`docs/plans/liberator-session-self-heal/ROADMAP.md`](ROADMAP.md) — feature roadmap
- Engine ROADMAP: [`docs/plans/ROADMAP.md`](../ROADMAP.md)
- Order-routing safety playbook: [`../../../.claude/playbooks/order-routing-safety.md`](../../../.claude/playbooks/order-routing-safety.md)

### Key Deliverables

1. **Enable monitor** — `enabled: true` + `auto_connect: true` in the mounted config.
2. **Self-heal E2E** — force dead → detect → relogin → OTP → iPhone forward → auto-confirm → alive.
3. **Fail-loud E2E** — relogin OTP not confirmed → `session.relogin_otp_timeout` alert + stays dead.
4. **Commit the `:8210` port-publish** (OTP webhook reachability).

---

## AI Prompt

Started by **"start Phase 5 enable monitor"**; the operator chose **"Enable + verify live now"**
(verify-then-commit) and, on the Sunday blocker, **"Override + test now (real OTP)"**, then **"Run
fail-loud live first"**. The spec is the ROADMAP's Phase 5 verification.

```
Enable the bundled SessionMonitorService (docker/liberator/session_status.yaml: enabled: true +
auto_connect: true) and prove the loop end-to-end on the running stack: force the session dead ->
the monitor detects it -> /login -> OTP SMS -> the operator's iPhone forwards it -> auto-confirm ->
alive; and the fail-loud path (OTP not confirmed -> session.relogin_otp_timeout alert + stays dead).
Verify-then-commit: commit the enablement only after the self-heal loop is proven live. Commit the
uncommitted docker-compose.liberator.yml :8210 port-publish alongside. No submodule change (the
machinery is pinned at 911255a). Mask all token/OTP/PIN contents.
```

---

## Scope

### In Scope (Phase 5)

| Deliverable | Description | Status |
|---|---|---|
| Enable monitor | `enabled: true` + `auto_connect: true` in the mounted config | Complete |
| Self-heal E2E | force dead → relogin → OTP → forward → confirm → alive (live) | Verified (3×) |
| Fail-loud E2E | OTP not confirmed → `session.relogin_otp_timeout` alert + stays dead (live) | Verified |
| Single-flight E2E | concurrent triggers → one OTP | Unit-proven; live race not forced |
| Compose port-publish | commit the `8210:8200` edit | Complete |

### Out of Scope

- Operator runbook / ops docs (**Phase 6**) — beyond the order-routing-safety reference.
- Any submodule / engine-`src` / infra-db change.

---

## Design Decisions

1. **Engine-config-only; no pin change.** The monitor auto-starts on boot when enabled (the liberator
   `lifespan` calls `monitor_service.start()`, which runs `_monitor_loop` only if `enabled`); the
   engine already pins the hardened submodule (`911255a`). So Phase 5 is just the mounted config flip
   + the compose port edit.
2. **Verify-then-commit (operator's choice).** The enablement was flipped locally and committed only
   after the live self-heal + fail-loud passed — the committed config is never armed unverified.
3. **The running container had to be rebuilt.** The deployed image was stale (built 2026-06-13, before
   Phases 3–4); it had to be rebuilt from `911255a` to run the Phase-4 code (`_await_otp_confirmation`,
   the `redis.asyncio` fix, `notify_service.py`). **Operators must rebuild the liberator container**
   when advancing the submodule pin — a pin bump alone doesn't redeploy the image.
4. **Server-side auto-confirm disable for the fail-loud test.** The operator's iPhone automation
   auto-forwards OTPs reliably (it forwarded every test OTP within ~7 s), so "phone off" is unreliable.
   The clean way to force fail-loud was `OTP_AUTO_CONFIRM_ENABLED=false` (env) — the OTP is received
   but not confirmed → the await times out → the alert fires. (Test-only; reverted.)

---

## Live E2E Evidence

**Self-heal (Phase-4 code, 2026-06-14):**
```
05:48:55  session DEAD detected → "triggering immediate login"
05:48:56  Login successful                  (real OTP SMS fired)
05:49:03  Received OTP SMS                   (the operator's iPhone forwarded it)
05:49:05  OTP confirmation successful + tokens stored
05:49:06  SUCCESS  Immediate login confirmed (OTP)   ← the Phase-4 _await_otp_confirmation path
          → session is_alive=True
```
Proven 3× total. **No "Redis session monitor connection failed"** on the Phase-4 code (the Phase-3
`redis.asyncio` fix; the stale image had logged 3 startup failures).

**Fail-loud (`OTP_AUTO_CONFIRM_ENABLED=false`, `RELOGIN_OTP_WAIT_SECONDS=25`):**
```
06:03:46  session DEAD detected → "triggering immediate login"
06:03:47  Login successful                  (real OTP SMS fired — exactly one)
06:03:54  Received OTP SMS                   (forwarded, but auto-confirm OFF → not confirmed)
06:04:12  ERROR  session.relogin_otp_timeout  [trigger=immediate_login wait_seconds=25]
          → session stayed DEAD (token absent); exactly one OTP fired (no storm)
```

**§C trading-hours gate:** today is Sunday — the monitor **paused itself** ("paused due to trading
hours automation"); the test required overriding `trading_hour.yaml` (Sunday → a 24 h session) +
clearing the Redis trading-config cache (`liberator:trading:config`, 24 h TTL) to un-pause it.

---

## File Changes (engine repo only)

| File | Action | Description |
|---|---|---|
| `docker/liberator/session_status.yaml` | MODIFY | `session_monitor.enabled`/`auto_connect` → `true` (cadence stays 300); comments updated |
| `docker-compose.liberator.yml` | MODIFY | commit the `8210:8200` port-publish (OTP-webhook reachability) |
| `docs/plans/liberator-session-self-heal/ROADMAP.md` | MODIFY | Phase 5 deliverables |
| `docs/plans/liberator-session-self-heal/phase5-enable-monitor.md` | CREATE | This plan + the live E2E evidence |
| `third_party/liberator-trading-api` (pin) | UNCHANGED | already `911255a` (Phase 4) |

> Test-only overrides (`trading_hour.yaml` Sunday session, `checking_time_interval: 20`, the env
> override) were reverted before the commit — **not** committed.

---

## Success Criteria

- [x] Monitor enabled (`enabled: true` / `auto_connect: true`) and auto-starts.
- [x] A dead session self-heals automatically via real OTP — no manual `/login` (verified live, 3×).
- [x] A missing/unconfirmed OTP → `session.relogin_otp_timeout` alert + the session stays dead — never
      a false "alive" (verified live).
- [x] Exactly one OTP per dead-session event (verified: one `Login successful` per attempt).
- [x] The §C trading-hours gate pauses the monitor outside trading hours (verified: Sunday).
- [x] The `:8210` OTP-webhook port-publish is committed.
- [x] No submodule / engine-`src` / infra-db change; no pin bump.

---

## Completion Notes

### Summary

The monitor is enabled and the self-heal + fail-loud loops are **proven live** on the Phase-4 code.
Verify-then-commit was honoured: the enablement was committed only after the loops passed. No pin
change (the hardened monitor is already pinned at `911255a`); the `:8210` port-publish landed here.

### Issues encountered / findings

1. **Stale deployed image.** The running liberator container was built 2026-06-13 (pre-Phase-3/4) and
   had to be **rebuilt from `911255a`** to run the Phase-4 code. A submodule pin bump does not
   redeploy the image — operators must `docker compose … build liberator-trading-api && … up -d`.
2. **Trading-hours override needed a Redis config-cache clear.** The trading-hours service caches its
   config in Redis (`liberator:trading:config`, 24 h TTL) and prefers it over the file; the Sunday
   override only took effect after clearing that key.
3. **The OTP auto-forward is reliable**, so the fail-loud test needed `OTP_AUTO_CONFIRM_ENABLED=false`
   (server-side) rather than "phone off."
4. **Runtime state after the test:** the monitor is enabled but **Sunday-paused**; the session is
   **dead** (a fail-loud-test artifact). It will auto-heal at the next market open (Monday) when the
   monitor un-pauses — or via a manual `POST /api/v1/login/`. Ensure the iPhone OTP-forward automation
   is running for the unattended Monday heal.
5. **Single-flight** was not forced as a precise live race (it is unit-proven and the lock infra —
   `redis.asyncio` — is live-healthy).

---

**Document Version:** 1.0
**Author:** AI Agent (Claude Opus 4.8)
**Status:** Complete
**Completed:** 2026-06-14

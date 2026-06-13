# quant-execution-engine — Feature ROADMAP: Liberator session self-heal

**Auto-login the Liberator broker session when it dies**, so order routing recovers without a
human manually re-running the 2FA login. This is a **refactor + enable** of the
`SessionMonitorService` that already ships (disabled) inside the bundled `liberator-trading-api`,
plus targeted hardening — **not** a greenfield build.

> Per-service feature plan under the **Execution engine**
> ([`../ROADMAP.md`](../ROADMAP.md)). The cross-cutting feature is
> [`feature-execution-engine`](../../../../plans/feature-execution-engine/ROADMAP.md). This plan
> touches the **bundled `liberator-trading-api` submodule** and the engine's
> `docker/liberator/*.yaml` config + docs — it does **not** change the frozen `NormalizedOrder`
> contract, the order state machine, the capability matrix, gating, or any infra-db schema.

**Status: Proposed (planning) — no code yet (2026-06-13).** Phases 1–6 are all `[ ]` not started.
The user asked for the plan first; implementation follows on approval.

---

## Status legend

| Symbol | Meaning |
|--------|---------|
| `[ ]` | Not started |
| `[~]` | In progress |
| `[x]` | Complete |
| `[-]` | Skipped / deferred |

---

## Context — why this exists

The Liberator broker session is a JWT with `exp ≈ 24h` and **no refresh token** — it dies every
day. When it does, the engine's `LiberatorAdapter` heartbeat fails, its circuit breaker trips
(`broker_circuit_open` + mass-cancel), and **all Liberator routing halts until someone logs back
in by hand**. Today nothing re-logs in automatically.

The capability to fix this already exists in the `liberator-trading-api` codebase (the same repo
backs both the operator's standalone checkout at `/home/batt/apps/trading/liberator-trading-api/`
and the bundled submodule `third_party/liberator-trading-api/`): a real `SessionMonitorService`
that polls liveness, and on a dead session calls `_attempt_immediate_login` / `_attempt_reconnection`,
with OTP auto-confirm closing the 2FA loop. It is **shipped disabled** and has a few correctness
and robustness gaps that must be fixed before it can be trusted to drive real re-logins.

### Verified findings that drive the design
- **Shipped disabled.** `docker/liberator/session_status.yaml` (the config the bundled service
  mounts) sets `session_monitor.enabled: false` + `auto_connect: false` — inert by design.
- **The liveness probe is broken for the bundled deployment.** Its `test_payload` carries
  placeholder `accountNo: '00000000'` / `pin: '000000'`, and the probe is an *order pre-place*
  (`POST /api/v1/order/pre-place/set`) → it would read **dead** even on a healthy session,
  causing false-positive re-login storms. Must be fixed first.
- **Duplicate config schemas.** Two overlapping blocks coexist —
  `session_status.monitoring` (`enabled: true`) and `session_monitor` (`enabled: false`) —
  with two different `enabled`/cadence keys. Confusing; consolidate.
- **No refresh token.** Every re-login fires a fresh OTP SMS. Full unattended self-heal therefore
  hard-depends on the operator's **always-on iPhone automation** forwarding the SMS to
  `POST /api/v1/otp/sms` (auto-confirm verified working without a pre-registered RefCode,
  2026-06-13).
- **Robustness gaps.** Fixed (non-exponential) backoff; **no single-flight lock** (the monitor
  and any manual trigger can race multiple logins → multiple OTPs); brittle string-match error
  detection; no operator alerting when a re-login can't complete.
- **The engine is a passive consumer.** `src/quant_execution_engine/adapters/liberator/{heartbeat,runtime}.py`
  + `adapters/session.py::SessionCircuitBreaker` run a 30s heartbeat + breaker (threshold 3) and
  recover automatically on the next good heartbeat. **No engine code change is required** for the
  chosen approach.

---

## Design decisions

| # | Decision | Rationale |
|---|----------|-----------|
| **D1** | **Liberator-owned self-heal.** The session lifecycle (detect dead → re-login → OTP auto-confirm) stays inside `liberator-trading-api`; the execution engine is untouched and recovers passively. | `liberator-trading-api` is the sole broker-credential owner; keeping the session lifecycle there respects the boundary and adds the least new surface. The engine breaker already handles "session down." |
| **D2** | **Read-only liveness probe.** Replace the order-pre-place probe with a read-only authed broker call (e.g. `GET /api/v1/profile`) needing only the stored auth token. | No account/PIN/secrets in mounted config, no order-shaped probe; works with just the token, so the bundled deployment can probe correctly. |
| **D3** | **Single-flight re-login** via a Redis SETNX lock on liberator-redis. | The monitor poll, a manual trigger, and any future engine nudge could all fire at once; only one login (one OTP) at a time. |
| **D4** | **Fail-loud on missing OTP.** If a triggered re-login isn't confirmed within a timeout, emit a structured alert and keep the session marked dead (engine breaker stays open). | No refresh token ⇒ a dead phone means the loop can't close; silent indefinite downtime is worse than a loud alert. |
| **D5** | **No frozen-contract / engine / infra-db change.** Liberator-internal code + engine config + docs only. | Keeps blast radius small; the engine's `NormalizedOrder`, breaker, gating, and the DB schema are untouched. |
| **D6** | **Submodule dual-commit.** Code lands in `third_party/liberator-trading-api` on its own branch (commit + push) *then* pin-bump in quant-execution-engine; the ROADMAP + `docker/liberator/*.yaml` live in the engine repo. | Mandatory rule for the nested submodule — never pin the parent against an unpushed submodule SHA. |

### Open questions
- **§A** Future hybrid: have the engine breaker nudge liberator `/login` on trip for
  faster-than-poll recovery. **Deferred** — the monitor's poll covers it; revisit if the
  5-minute detection latency proves too slow.
- **§B** Confirm what `LiberatorAdapter.heartbeat()` actually probes — liberator `/health`
  (always up even with a dead broker session) vs. true broker-session liveness. If only
  `/health`, the engine breaker won't trip on a dead *broker* session and the liberator monitor's
  detection is the sole signal (acceptable, but must be documented).
- **§C** Trading-hours gating of re-login (don't burn OTPs overnight) — reuse the monitor's
  existing `_check_trading_hours_automation` pause/resume.

---

## Phases

### Phase 1 — Liveness probe correctness `[ ]` (prerequisite)
Goal: detection must be trustworthy before auto-login is enabled.

| Deliverable | Status | Notes |
|---|---|---|
| Read-only liveness probe (D2) | `[ ]` | `app/services/session_status_service.py::check_session_status` calls a read-only authed endpoint (e.g. `GET /api/v1/profile`) instead of `POST /api/v1/order/pre-place/set`; alive ⇔ 2xx with a valid auth token |
| Drop placeholder-cred dependency | `[ ]` | Remove the `accountNo`/`pin` `test_payload` requirement from the probe path |
| Unit tests | `[ ]` | alive / 401-or-403 / no-token / network-error → correct `is_alive` |

### Phase 2 — Config consolidation `[ ]`
Goal: one monitoring schema, clearly documented.

| Deliverable | Status | Notes |
|---|---|---|
| Single `SessionMonitorConfig` schema | `[ ]` | Collapse `session_status.monitoring` + `session_monitor` into one block in `app/models/session_status.py`; one `enabled`, one cadence |
| Update example/sample configs | `[ ]` | `config/session_status.yaml.example` + samples reflect the consolidated schema |
| Update mounted config | `[ ]` | `docker/liberator/session_status.yaml` migrated to the new schema (still disabled until Phase 5) |

### Phase 3 — Re-login hardening `[ ]`
Goal: safe, non-racing, backed-off re-login. Reuse the existing loop/methods — do **not** rewrite.

| Deliverable | Status | Notes |
|---|---|---|
| Single-flight lock (D3) | `[ ]` | Redis SETNX (liberator-redis) around the re-login critical section in `_attempt_immediate_login` / `_attempt_reconnection`; lock has a TTL so a crashed login self-clears |
| Exponential backoff + jitter | `[ ]` | Replace fixed `retry_backoff_seconds` with capped exponential + jitter |
| Trading-hours respect (§C) | `[ ]` | Re-login attempts honor `_check_trading_hours_automation` so OTPs aren't fired when markets are closed |
| Unit tests | `[ ]` | concurrent triggers → one login; backoff schedule; trading-hours pause |

### Phase 4 — Fail-loud OTP fallback + alerting `[ ]`
Goal: a dead phone is loud, not silent. Implements D4.

| Deliverable | Status | Notes |
|---|---|---|
| "Awaiting OTP" tracking | `[ ]` | After a triggered login, watch for a confirmed OTP (via `OTPIntegrationService` result / `current_attempt_counts` / token refresh) within a timeout |
| Pluggable notifier | `[ ]` | NEW `app/services/notify_service.py` — structured `session.relogin_otp_timeout` log + optional webhook hook (no secrets) |
| Stay-dead on timeout | `[ ]` | On OTP timeout, keep the session marked dead so the engine breaker stays open (no false "alive") |
| Settings | `[ ]` | `app/config.py`: OTP-wait timeout + notifier target |
| Unit tests | `[ ]` | OTP-confirmed-in-time → alive; OTP-timeout → alert fired + stays dead |

### Phase 5 — Enable in the bundled deployment + end-to-end verification `[ ]`
Goal: turn it on and prove the full loop.

| Deliverable | Status | Notes |
|---|---|---|
| Enable monitor | `[ ]` | `docker/liberator/session_status.yaml`: `enabled: true` + `auto_connect: true` with the real read-only probe |
| Self-heal E2E | `[ ]` | force session dead → detect → `/login` → OTP SMS → iPhone forward → auto-confirm → alive → engine breaker recovers |
| Fail-loud E2E | `[ ]` | phone unreachable → OTP timeout → alert + session stays dead + breaker stays open |
| Single-flight E2E | `[ ]` | concurrent dead-detections → exactly one `/login` / one OTP |

### Phase 6 — Docs / runbook `[ ]`
| Deliverable | Status | Notes |
|---|---|---|
| Operator runbook | `[ ]` | `../../../.claude/playbooks/order-routing-safety.md` + umbrella `../../../../.claude/playbooks/execution-engine-runbook.md`: self-heal behavior, the **always-on iPhone automation dependency**, the fail-loud alert response |
| Ops docs | `[ ]` | `../../operations/*` — new monitor config keys + enable/disable |
| Note in engine `CLAUDE.md` | `[ ]` | `../../../CLAUDE.md` once shipped |

---

## Success criteria
- [ ] A dead Liberator session is detected and re-logged-in automatically (markets open), with the
      engine breaker recovering on the next heartbeat — no manual `/login`.
- [ ] Only one re-login / one OTP fires per dead-session event, even under concurrent triggers.
- [ ] A missing OTP (phone off) produces a structured alert and the session stays dead — never a
      false "alive".
- [ ] The liveness probe needs no account number or PIN and reads correctly on a healthy session.
- [ ] One monitoring config schema; no duplicate `enabled` flags.
- [ ] No change to the frozen `NormalizedOrder` / state machine / capability cells / gating /
      infra-db schema; engine code unchanged (D1, D5).
- [ ] Submodule quality gate green (`ruff` / `mypy` / `pytest` ≥90%); dual-commit + pin bump (D6).

## Verification (Phase 5, end-to-end)
1. Bring up infra-db → execution-engine (owner mode) → liberator overlay with the monitor enabled.
2. Successful login (verified flow): `POST /api/v1/login/` → iPhone forwards OTP → confirm →
   `GET /api/v1/session/status` = alive.
3. **Self-heal:** force the session dead (clear/expire/invalidate the stored token) → monitor
   detects within `checking_time_interval` → triggers `/login` → OTP SMS → iPhone forwards →
   auto-confirm → alive again → engine breaker recovers.
4. **Fail-loud:** trigger a re-login with the phone unreachable → after the timeout, assert a
   `session.relogin_otp_timeout` alert + session stays dead + breaker stays open.
5. **Single-flight:** force concurrent dead-detections → assert exactly one `/login` / one OTP.
6. Submodule unit tests for backoff, lock, fail-loud timeout, and the read-only probe.

---

## Reuse (do not rebuild)
- `SessionMonitorService._monitor_loop` / `_check_and_reconnect` / `_attempt_immediate_login` /
  `_attempt_reconnection` — harden in place.
- `OTPIntegrationService.process_sms_with_auto_confirm` + `current_attempt_counts` — the OTP
  auto-confirm path.
- `liberator-redis` — the single-flight lock + the existing expected-RefCode/OTP state.
- `TradingHoursService` / `_check_trading_hours_automation` — trading-hours pause (§C).
- Engine `adapters/liberator/{heartbeat,runtime}.py` + `adapters/session.py` — **unchanged**;
  document the passive-recovery interaction only.

## Dependencies / notes
- **Hard human-in-the-loop dependency:** unattended self-heal requires the operator's iPhone
  OTP-forward automation running 24/7 (no refresh token). Phase 4's fail-loud alert is the safety
  net for when it isn't.
- The `docker-compose.liberator.yml` host-port edit (`8210:8200`, added 2026-06-13 so the OTP
  webhook is reachable via Cloudflare) is still **uncommitted** — orthogonal to this feature but
  should be committed alongside Phase 5.

## Cross-references
- Engine ROADMAP: [`../ROADMAP.md`](../ROADMAP.md)
- LiberatorAdapter (Phase 3) plan: [`../phase3-liberator-adapter.md`](../phase3-liberator-adapter.md)
- Order-routing safety playbook: [`../../../.claude/playbooks/order-routing-safety.md`](../../../.claude/playbooks/order-routing-safety.md)
- Cross-cutting feature: [`../../../../plans/feature-execution-engine/ROADMAP.md`](../../../../plans/feature-execution-engine/ROADMAP.md)

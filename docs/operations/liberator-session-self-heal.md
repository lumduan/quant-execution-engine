# Operations — Liberator session self-heal (auto-relogin)

The bundled `liberator-trading-api` upstream **auto-logs-in its own broker session when it dies**, so
Liberator order routing recovers without a human manually re-running the 2FA login. This is the
`SessionMonitorService` inside `third_party/liberator-trading-api`; it is **enabled** in the bundled
deployment as of **2026-06-14** (Phase 5, verified live). This page is the operator reference: what it
does, the **hard iPhone-automation dependency**, the **fail-loud alert response**, how to enable /
disable it, the full config surface, and the two live deployment gotchas.

> **Scope.** This is **Liberator-only** and lives entirely inside the bundled upstream (the sole
> broker-credential owner). The execution engine's `NormalizedOrder` contract, state machine,
> capability cells, gating, kill-switch, and PTRM are **unchanged** — the engine recovers *passively*
> (see [Engine-breaker relationship](#relationship-to-the-engine-circuit-breaker)). Settrade has no
> equivalent: it is OAuth app-credentials with no OTP, so its client re-logins on its own (see the
> safety playbook's Settrade runbook).

## Why this exists

The Liberator broker session is a JWT with `exp ≈ 24 h` and **no refresh token** — it dies every day.
When it does, the engine's `LiberatorAdapter` heartbeat fails, its circuit breaker trips
(`broker_circuit_open` + mass-cancel), and **all Liberator routing halts until someone logs back in by
hand**. The monitor closes that loop automatically — but because there is no refresh token, **every**
re-login fires a fresh OTP SMS, so unattended self-heal hard-depends on the operator's always-on phone
automation forwarding that SMS.

## The self-heal loop

```
every checking_time_interval (300 s):
  GET /api/v1/profile        ← read-only liveness probe (auth-token only; no account/PIN)
    └─ 2xx  → alive, do nothing
    └─ dead → single-flight lock (Redis SETNX) → POST /api/v1/login/   ← fires ONE OTP SMS
                 │
                 ▼   the operator's iPhone forwards the SMS within seconds
              POST /api/v1/otp/sms  → auto-confirm → fresh token stored
                 │
                 ▼
              "Immediate login confirmed (OTP)"  → session alive again
```

1. **Probe** — a read-only `GET /api/v1/profile` every `checking_time_interval` (default **300 s**).
   It needs only the stored auth token — no account number, no PIN, no order-shaped payload (Phase 1
   fixed a prior order-pre-place probe that false-read *dead* on a healthy session).
2. **Detect dead → trigger one login.** On a non-2xx probe the monitor takes a **single-flight Redis
   SETNX lock** (`liberator:session:relogin_lock` on liberator-redis, token-guarded + TTL, fail-open on
   a Redis outage) so the poll, a manual trigger, and any future nudge cannot fire **two** logins (two
   OTPs). It then calls `POST /api/v1/login/`, which **fires exactly one OTP SMS**.
3. **OTP forward → auto-confirm.** The operator's iPhone automation forwards the SMS to
   `POST /api/v1/otp/sms`; auto-confirm closes the 2FA loop (no pre-registered RefCode needed) and
   stores a fresh token.
4. **Confirmed.** The monitor polls for the new token and logs **`Immediate login confirmed (OTP)`**
   (or `Session reconnection confirmed (OTP)` on the reconnection path) → the session is alive again.
5. **Backoff between failed attempts** — exponential with equal jitter, base `retry_backoff_seconds`
   (60 s) capped at `retry_backoff_max_seconds` (300 s).

## The iPhone-automation dependency (hard, human-in-the-loop)

**There is no refresh token**, so the loop above **cannot close without the OTP being forwarded**.
Unattended self-heal therefore **requires the operator's iPhone OTP-forward automation to be running
24/7** (it forwards the SMS that arrives at the operator's phone to `POST /api/v1/otp/sms`). If that
automation is off, the monitor will trigger a login, the OTP will arrive on the phone, and nothing will
confirm it — which is exactly what the fail-loud path handles next.

## Fail-loud — when the OTP isn't confirmed (D4)

If a triggered re-login's OTP is **not** confirmed within `relogin_otp_wait_seconds` (default
**300 s**, polled every `relogin_otp_poll_seconds` = 5 s), the monitor **fails loud and stays dead** —
it never reports a false "alive":

- A structured **ERROR** alert fires: `session.relogin_otp_timeout`
  (`trigger=immediate_login|reconnection wait_seconds=N`), via `NotifyService` — always logged, plus an
  optional best-effort webhook if `RELOGIN_NOTIFY_WEBHOOK_URL` is set (never raises, no secrets in the
  payload).
- The session **stays dead** (the token is absent), `_consecutive_failures` increments, and the monitor
  does **not** immediately re-trigger — so a dead phone produces **one** OTP + a loud alert, not an OTP
  storm.

### Operator response to `session.relogin_otp_timeout`

1. **Check the iPhone OTP-forward automation** — is it running and online? This alert almost always
   means the OTP reached the phone but wasn't forwarded to `POST /api/v1/otp/sms`.
2. Once the phone is healthy, the next poll (or a manual `POST /api/v1/login/`) re-triggers; confirm
   recovery with `GET /api/v1/session/status` = alive.
3. While dead, Liberator routing stays halted and the engine breaker stays open — **correct**: no
   false "alive", no real order routed on a dead session.

## The §C trading-hours gate

The monitor honours trading hours: outside them it **pauses itself**, logging *"Session monitor service
paused due to trading hours automation"* — so it does **not** burn OTPs overnight or on weekends /
holidays. **Consequence:** a session that dies over a weekend **stays dead until the next market open**,
then auto-heals on the first in-hours poll. (Verified live: on a Sunday the monitor paused; the Phase-5
test had to override trading hours to exercise the loop — see [Gotcha ②](#gotcha--trading-hours-config-is-redis-cached).)

## Enable / disable

The monitor reads the mounted `docker/liberator/session_status.yaml`. It is **enabled** in the bundled
deployment:

```yaml
session_monitor:
  enabled: true          # master switch — the monitor loop runs only when true
  auto_connect: true     # auto-trigger login on a dead session (vs. monitor-only)
  checking_time_interval: 300
  # … (full surface below)
```

- **To disable** (monitor-only or fully off): set `auto_connect: false` (probe + alert, never logs in)
  or `enabled: false` (no loop at all), then **rebuild + restart** the liberator container (the config
  is read at start — see [Gotcha ①](#gotcha--a-pin-bump-does-not-redeploy-the-image)). Disabling reverts
  to the pre-2026-06-14 behaviour: a dead session halts Liberator routing until a manual OTP login.
- The monitor auto-starts on boot when `enabled: true` (the liberator `lifespan` calls
  `monitor_service.start()`, which runs the loop only if enabled).

## Configuration surface

Two layers. The cadence/lock/backoff live in the **mounted YAML**; the fail-loud OTP knobs are
**liberator-container env vars** (the bundled app's own settings — **not** `EXECUTION_ENGINE_`-prefixed,
and distinct from the engine's `EXECUTION_ENGINE_LIBERATOR_*`).

### `docker/liberator/session_status.yaml` → `session_monitor.*`

| Key | Default | Effect |
|-----|---------|--------|
| `enabled` | `true` | Master switch — the monitor loop runs only when true. |
| `auto_connect` | `true` | Auto-trigger `POST /api/v1/login/` on a dead session (false = monitor-only, no login). |
| `checking_time_interval` | `300` | Seconds between liveness probes (detection latency upper bound). |
| `max_retries` | `3` | Re-login attempts per dead-session event before backing off harder. |
| `retry_backoff_seconds` | `60` | Backoff base (exponential + equal jitter). |
| `retry_backoff_max_seconds` | `300` | Backoff cap. |
| `notify_on_reconnect` | `true` | Emit a structured notice on a successful reconnect. |
| `max_consecutive_failures` | `5` | Failures before the service pauses itself. |
| `service_pause_minutes` | `30` | Pause duration after `max_consecutive_failures`. |
| `health_check_on_startup` | `false` | Probe immediately on boot (vs. waiting one interval). |
| `log_level` | `info` | Monitor log verbosity. |

### Liberator-container env (the bundled app's `.env`, no prefix)

| Env var | Default | Effect |
|---------|---------|--------|
| `RELOGIN_OTP_WAIT_SECONDS` | `300` | Seconds to wait for a triggered re-login's OTP to confirm before failing loud (10–900). |
| `RELOGIN_OTP_POLL_SECONDS` | `5` | Poll interval while awaiting OTP confirmation (1–60). |
| `RELOGIN_NOTIFY_WEBHOOK_URL` | `""` | Optional webhook for the `session.relogin_otp_timeout` alert (best-effort; no secrets in payload). |
| `OTP_AUTO_CONFIRM_ENABLED` | `true` | Auto-confirm a forwarded OTP. **Must be `true` for unattended self-heal** (the Phase-5 fail-loud test set it `false` server-side to force a timeout). |

## Live deployment gotchas

### Gotcha ① — a pin bump does NOT redeploy the image

The liberator container is **built from the submodule source**. Advancing the
`third_party/liberator-trading-api` pin (or editing the mounted config) does **not** rebuild the running
image — operators must rebuild it:

```bash
docker compose -f docker-compose.yml -f docker-compose.private.yml -f docker-compose.liberator.yml \
  build liberator-trading-api
docker compose -f docker-compose.yml -f docker-compose.private.yml -f docker-compose.liberator.yml \
  up -d
```

> Phase 5 hit this directly: the running container was a stale 2026-06-13 image (pre-Phases 3–4) and
> logged the *old* "Immediate login successful" instead of the Phase-4 "Immediate login confirmed
> (OTP)" until it was rebuilt from the hardened pin.

### Gotcha ② — trading-hours config is Redis-cached

The trading-hours service caches its config in Redis (`liberator:trading:config`, **24 h TTL**) and
**prefers the cache over the file**. So editing `broker-api/docker/liberator/trading_hour.yaml` alone
may not take effect — clear the cache key too:

> ⚠️ **Path corrected 2026-09-06.** This said `docker/liberator/…`, relative to *this* repo, which
> was right only while the bridge was nested under it. Since the de-nesting the live file is the
> umbrella's `broker-api/docker/liberator/trading_hour.yaml`; this repo's copy was a stale vendor
> **sample** whose holiday calendar held 2025 data and **no 2026 dates at all**, and it has been
> deleted (see `docker/liberator/README.md`).

```bash
docker exec liberator-redis redis-cli DEL liberator:trading:config
```

This matters mainly for **testing** the §C gate outside market hours; in normal operation the gate just
pauses the monitor on weekends / holidays as intended.

## Single-flight (lock) — what's proven

The single-flight Redis SETNX lock is **unit-proven** (concurrent triggers → one login / one OTP) and
its infra (`redis.asyncio`) was live-healthy in Phase 5; a precise live race was **not** force-tested.
Treat "exactly one OTP per dead-session event" as designed-and-unit-verified, holding under the live
runs observed (one `Login successful` per attempt).

## Relationship to the engine circuit breaker

The execution engine is a **passive consumer**: `adapters/liberator/{heartbeat,runtime}.py` +
`adapters/session.py::SessionCircuitBreaker` run a ~30 s heartbeat + breaker and **recover
automatically on the next good heartbeat** once the liberator session is restored — no engine code is
involved in the self-heal. In Phase 5 the engine ran in **sim** mode, so engine-breaker recovery is the
**adapter-mode** behaviour (it applies when the engine runs the Liberator adapter at `paper` /
`micro_live`); it was not exercised by the sim-mode live test. The breaker-trip runbook is in the safety
playbook ([Circuit-breaker trip runbook](../../.claude/playbooks/order-routing-safety.md#circuit-breaker-trip-runbook)).

## Verified Phase-5 evidence (2026-06-14)

**Self-heal** (proven 3×):

```
05:48:55  session DEAD detected → "triggering immediate login"
05:48:56  Login successful                 (real OTP SMS fired — exactly one)
05:49:03  Received OTP SMS                  (the operator's iPhone forwarded it)
05:49:05  OTP confirmation successful + tokens stored
05:49:06  SUCCESS  Immediate login confirmed (OTP)   → session is_alive=True
```

**Fail-loud** (`OTP_AUTO_CONFIRM_ENABLED=false`, `RELOGIN_OTP_WAIT_SECONDS=25`):

```
06:03:46  session DEAD detected → "triggering immediate login"
06:03:47  Login successful                 (one OTP fired)
06:03:54  Received OTP SMS                 (forwarded, but auto-confirm OFF → not confirmed)
06:04:12  ERROR  session.relogin_otp_timeout  [trigger=immediate_login wait_seconds=25]
          → session stayed DEAD; exactly one OTP fired (no storm)
```

Full run notes: [`../plans/liberator-session-self-heal/phase5-enable-monitor.md`](../plans/liberator-session-self-heal/phase5-enable-monitor.md).

## Cross-references

| Resource | Path |
|----------|------|
| Feature roadmap (Phases 1–6) | [`../plans/liberator-session-self-heal/ROADMAP.md`](../plans/liberator-session-self-heal/ROADMAP.md) |
| Phase 5 live-verification notes | [`../plans/liberator-session-self-heal/phase5-enable-monitor.md`](../plans/liberator-session-self-heal/phase5-enable-monitor.md) |
| Order-routing safety playbook (Liberator runbook) | [`../../.claude/playbooks/order-routing-safety.md`](../../.claude/playbooks/order-routing-safety.md) |
| Troubleshooting (the alert entry) | [`troubleshooting.md`](troubleshooting.md) |
| Umbrella operator runbook | [`../../../.claude/playbooks/execution-engine-runbook.md`](../../../.claude/playbooks/execution-engine-runbook.md) |

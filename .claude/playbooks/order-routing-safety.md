# Playbook — order-routing safety

> Order routing is **irreversible and outward-facing**. This checklist gates every change that
> could let an order reach a real broker. It is the operational companion to the ROADMAP's
> "Safety ladder" section and hard rules. Most items are **Proposed** until the matching phase
> lands; the checklist itself is binding from Phase 2 onward.

## Before raising the stage toward `live`

1. **Kill-switch reachable + tested.** `EXECUTION_ENGINE_KILL_SWITCH_ENGAGED=true` (or the admin
   trip) rejects every new submit with a typed error and flattens — verified by a fault-injection
   test (Phase 6). The submit path checks it **first**.
2. **Stage is explicit.** `EXECUTION_ENGINE_STAGE` defaults to `sim`. Promote one rung at a time
   (`sim → paper → micro_live → live`); never skip to `live`. `micro_live` caps to the smallest
   venue size.
3. **Owner mode only.** `EXECUTION_ENGINE_PUBLIC_MODE=false` is required for any submit; public
   mode answers only health / capabilities / reads.
4. **Risk-gate caps configured (Phase 6 hardening).** Per-account notional / qty caps
   (`ACCOUNT_MAX_NOTIONAL` / `ACCOUNT_MAX_QTY` — an account absent from the map falls back to the
   global `RISK_MAX_*`, never a silent skip) and the optional advisory price-band
   (`PRICE_BAND_ENABLED`) are set before `micro_live`. The **unified duplicate-burst guard is
   default-ON** (`DUPLICATE_BURST_GUARD_ENABLED=true`): a second order with the same
   `account|symbol|side|qty|order_type|price` fingerprint under a **different** `client_order_id`
   inside `DUPLICATE_BURST_WINDOW_SECONDS` is rejected `409 duplicate_burst_detected` (a same-cid
   resend stays idempotency dedupe, never here). The risk gate runs in **every** stage, including
   `sim`.
5. **Idempotency proven.** Re-submitting a `client_order_id` returns the prior ack — confirmed by
   the dedupe soak test. No double-send under a mid-submit process kill.
6. **Reconciliation green.** The reconciliation loop matches broker truth ↔ local state with no
   unresolved drift; a dead broker session is detected by the health path.

## Secret hygiene (every change)

- Broker secrets (Liberator PIN, the Streaming Pro bridge api-key) live **only** in the gitignored
  `.env`. Never commit, never log. `git status` must be clean of `.env`; `.env.example` carries
  placeholders only.
- Never log a PIN, token, full account number, or raw broker payload. Raw broker responses
  (`NormalizedOrderResult.raw`) are private-only and never cross the public boundary.

## Pre-push gate (matches CI)

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
```

Re-run `ruff format --check` after any post-format edit. Do not push red. Coverage ≥90% on
`adapters/` + the order state machine.

## When adding a broker adapter

1. Implement the full `BrokerAdapter` interface; **declare its capability set** (don't pretend
   to support what the venue can't).
2. Map status / reject codes → the normalized status enum; never swallow a reject.
3. Keep the auth/session inside the adapter (D10); surface session-dead to health/reconcile.
4. Add adapter tests with the broker HTTP/SDK **mocked** — **no live credentials in CI**.

## Liberator specifics (Phase 3)

### Bring-up (owner mode + bundled upstream)

```bash
# Public/sim default stays broker-free; liberator only joins via the overlay:
docker compose -f docker-compose.yml -f docker-compose.private.yml -f docker-compose.liberator.yml up -d
```

- Adapter target: `http://liberator-trading-api:8200/api/v1` (internal-only; no host port).
- Engine-side config: `EXECUTION_ENGINE_LIBERATOR_BASE_URL` / `_API_KEY` / `_PIN` (the api-key
  must equal the upstream container's `API_KEY`). Upstream creds (`LIBERATOR_USERNAME/PASSWORD`,
  `TEL_NO`, `API_KEY`, `LIBERATOR_PIN`) come from the same gitignored `.env`.
- The runtime only starts when `stage ∈ {paper, micro_live, live}` AND owner mode AND both
  engine-side secrets are present; missing secrets log a WARNING and leave liberator routing
  disabled (`micro_live` submits then 403 `stage_rejected`).
- **The broker session needs an operator OTP login** (SMS): `POST /api/v1/login/` +
  `/login/confirm-otp` on the upstream (engine-internal network). Until the auth token exists,
  the heartbeat reports session-dead and the breaker trips — that is correct behaviour.

### Secret hygiene (Liberator)

- `EXECUTION_ENGINE_LIBERATOR_PIN` / `_API_KEY` are `SecretStr`; only
  `adapters/liberator/transport.py` touches the wire and it logs method/path/status plus a
  **redacted** payload (`pin`/`accountNo` masked). A caplog test pins "PIN never logged" —
  keep it green on every adapter change.

### Circuit-breaker trip runbook

1. Trip signal: `GET /health` shows `brokers.liberator.breaker_state = "open"`; new
   `broker=liberator` submits return 503 `broker_circuit_open`; the trip already attempted a
   best-effort **mass-cancel** of open orders (check the engine log for the sweep result).
2. Diagnose the session: upstream `GET /api/v1/order/health/set` — `auth_token_available:
   false` means the OTP session died → re-login (OTP via SMS webhook). HTTP failures mean the
   liberator container itself is down (`docker compose ... ps`, logs).
3. Recovery is automatic: the next healthy heartbeat resets the breaker (state `closed`).
   Reconcile then repairs any drift; verify with `GET /orders/{client_order_id}` on anything
   that was in flight.
4. If the venue is healthy but you must halt anyway, use the kill-switch
   (`POST /admin/kill-switch/engage`) — it rejects new submits AND mass-cancels.

### Session self-heal (auto-relogin) — **enabled** since 2026-06-14

The bundled `liberator-trading-api` upstream **auto-relogs-in its own dead broker session** (the
`SessionMonitorService`, enabled in `docker/liberator/session_status.yaml`), so step 2 of the breaker
runbook above ("re-login (OTP via SMS webhook)") now usually happens **without a human**. The loop: a
read-only `GET /api/v1/profile` probe every `checking_time_interval` (300 s) → on dead, a single-flight
(Redis SETNX) `POST /api/v1/login/` firing **one** OTP SMS → the operator's iPhone forwards it to
`POST /api/v1/otp/sms` → auto-confirm → `Immediate login confirmed (OTP)` → alive; the engine breaker
then recovers on its next good heartbeat.

- **Hard dependency:** there is **no refresh token**, so unattended self-heal **requires the operator's
  iPhone OTP-forward automation running 24/7**. If it's off, the monitor **fails loud**: a structured
  ERROR `session.relogin_otp_timeout` fires and the **session stays dead** (never a false "alive") —
  one OTP + one alert, no storm. **Operator response: check the phone automation.**
- **§C trading-hours gate:** the monitor pauses outside trading hours, so a session that dies over a
  weekend stays dead until market open, then auto-heals.
- **Enable/disable:** `session_monitor.enabled` / `auto_connect` in `docker/liberator/session_status.yaml`
  (fail-loud knobs are the liberator container's own env — `RELOGIN_OTP_WAIT_SECONDS`,
  `OTP_AUTO_CONFIRM_ENABLED`, …). **Rebuild the liberator image after any change** — a pin bump or config
  edit does NOT redeploy it.
- **Full reference:** [`../../docs/operations/liberator-session-self-heal.md`](../../docs/operations/liberator-session-self-heal.md).

### Stage-flip rule (cancel routing is stage-at-call-time)

Cancels resolve their adapter from the CURRENT stage, so flipping
`EXECUTION_ENGINE_STAGE` while orders are open mis-routes their cancels (e.g. a real
micro_live order can no longer be cancelled after dropping to sim). **Before changing the
stage: engage the kill-switch (mass-cancels everything), verify `GET /health`, then flip and
disengage.** The reconciler repairs drift on flip-back, but never rely on it for this.

### Lost-ack / reconcile notes

- A submit that dies between the durable PENDING_NEW insert and the venue ack is the designed
  lost-ack window: the reconciler fuzzy-matches `(account, symbol, side, qty)` ±5 s and acks;
  unmatched past 60 s resolves to `REJECTED "ack_lost_unmatched"`. It NEVER re-sends — re-submit
  with a fresh `client_order_id` after confirming venue state.
- TFEX stop orders ship `stopCondition: ""` in v1 — the venue's condition vocabulary is pinned
  during the operator-driven micro_live validation; a venue reject flows back typed.

## Broker specifics — Settrade removed (2026-07-18)

> The Settrade Open-API broker (broker-023 / `settrade_v2`) was **removed** — adapter, config, and its runbook are gone. Real-money routing is now **Liberator** + **Streaming Pro** (the self-built retail bridge). Streaming Pro follows the general adapter runbook above (circuit-breaker trip, stage-flip rule, cancel/`cancel_replace` amend); it holds **no PIN** (the bridge owns login/OTP/session and stamps the PIN itself). See [`decision-log.md`](../knowledge/decision-log.md) → the 2026-07-18 removal entry.

## Kill-switch trip procedure (Phase 6)

The runtime kill-switch is the real-money **flatten-and-halt**: it rejects every new submit with
a typed error AND mass-cancels open orders. It is owner-mode + API-key, engine-direct (never
proxied), and the env flag (`EXECUTION_ENGINE_KILL_SWITCH_ENGAGED`) is the boot-time backstop that
**pins over** a runtime disengage. Phase 6 hardened the admin trip to be idempotent,
structured-logged, and operator-attributed.

### Engage (trip it)

```bash
# Owner-mode + API-key; optional X-Operator-Id for the audit log.
curl -X POST http://localhost:8400/admin/kill-switch/engage \
  -H "X-API-Key: $EXECUTION_ENGINE_API_KEY" \
  -H "X-Operator-Id: alice"      # optional; logged as "anonymous" if omitted
```

- **Response** (`200`): `{"engaged": true, "already_engaged": false, "cancelled_count": N,
  "cancelled": [<cid>...], "failed": [<cid>...]}`. `cancelled_count == len(cancelled)`; `failed`
  lists cids the best-effort sweep could not cancel (diagnose those individually).
- **Structured log:** one JSON line `{"event": "kill_switch.engaged", "operator": "...",
  "cancelled_count": N, "failed_count": M}` — grep the engine log for `kill_switch.engaged`. Never
  any secret.
- **Expected audit trail:** each open order is swept through the frozen cancel path, so
  `execution.order_events` gains a `PENDING_CANCEL → CANCELLED` pair per order (the genuine
  append-only rows — there is **no** literal `kill_switch_cancel` event_type; the kill-switch
  framing lives in the structured log + `cancelled_count`). Confirm via
  `GET /admin/orders/{cid}/audit` (below) or a date-range export.
- **Idempotent:** engaging again returns `200` with `already_engaged=true` and
  `cancelled_count=0` — it runs **no second sweep**. Safe to retry.

### Disengage (clear the runtime trip)

```bash
curl -X POST http://localhost:8400/admin/kill-switch/disengage \
  -H "X-API-Key: $EXECUTION_ENGINE_API_KEY" -H "X-Operator-Id: alice"
```

- **Response** (`200`): `{"engaged": false, "source": "..."}`. Emits a structured
  `kill_switch.disengaged` log with the operator identity.
- **Already clear → `409 kill_switch_not_engaged`** (distinct from the env-pinned conflict).
- **Env-pinned wins:** if `EXECUTION_ENGINE_KILL_SWITCH_ENGAGED=true`, disengage raises
  `409 kill_switch_env_pinned` — the runtime cannot override the env flag; clear the env flag and
  restart to truly disengage.
- After disengage, a fresh submit is accepted again; verify with a sim order or `GET /health`.

> **Stage-flip precedent reaffirmed.** This trip is exactly the tool the stage-flip rule mandates:
> **before changing `EXECUTION_ENGINE_STAGE` while orders are open, engage the kill-switch
> (mass-cancels everything) first, verify `GET /health`, then flip and disengage.** Verified
> end-to-end by the Phase 6 5-order fault-injection test (engage → all CANCELLED + audit rows →
> fresh submit rejected → disengage → fresh submit accepted).

## Audit export procedure (Phase 6)

Two owner-mode reads over the append-only `execution.order_events` store, for reconciliation and
post-incident review. Both are **synthesized** from the existing columns — there is **no
`quant-infra-db` schema change** — and are reads only (no DB write). `403 public_mode` outside
owner mode.

### Single-order trail

```bash
curl http://localhost:8400/admin/orders/{client_order_id}/audit \
  -H "X-API-Key: $EXECUTION_ENGINE_API_KEY"
```

- Returns the order header (`client_order_id`/`broker`/`symbol`) plus its **ordered** events; each
  event carries the synthesized `seq` (1-based per-order ordinal), `from_status`/`to_status`,
  `event_type` (a derived `(from,to)` label — `create`/`ack`/`replace`/`fill`/`cancel_request`/
  `cancel`/`replace_request`/`reject`/`expire`), `broker_order_id` + opaque `metadata` (both from
  the stored `event` JSONB), and `occurred_at` (UTC ISO-8601).
- `404 order_not_found` when the cid is not in `execution.orders`.

### Date-range NDJSON export (reconciliation)

```bash
# Streaming NDJSON (one JSON object per line); from_ts inclusive, to_ts exclusive.
curl "http://localhost:8400/admin/audit/export?from_ts=2026-06-13T00:00:00Z&to_ts=2026-06-14T00:00:00Z&strategy_id=csm-set" \
  -H "X-API-Key: $EXECUTION_ENGINE_API_KEY" -o audit_2026-06-13_2026-06-14.ndjson
```

- `Content-Type: application/x-ndjson`; a `Content-Disposition: attachment; filename="audit_<lo>_<hi>.ndjson"`
  names the range (`all` when a bound is omitted).
- Filters (all optional): `from_ts` (inclusive `created_at >=`), `to_ts` (exclusive
  `created_at <`), `strategy_id` (joins `execution.orders.strategy_id` — the D16 attribution
  column; needs the strategy to have stamped `X-Strategy-Id`).
- **Streams as fetched** via a server-side cursor (500-row batches) — a large date range never
  buffers in memory. Each line carries `event_id`/`client_order_id`/`from_status`/`to_status`/the
  decoded `event` object/`strategy_id`/`created_at`.
- Use it to diff engine truth against a venue statement for a trading day; the `event_id` order is
  total and stable even for events that share a `created_at` inside one transaction.

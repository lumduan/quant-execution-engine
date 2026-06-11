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
4. **Per-account caps configured.** Notional / qty / price-band / duplicate-burst caps are set
   for the account before `micro_live` (Phase 6 hardening).
5. **Idempotency proven.** Re-submitting a `client_order_id` returns the prior ack — confirmed by
   the dedupe soak test. No double-send under a mid-submit process kill.
6. **Reconciliation green.** The reconciliation loop matches broker truth ↔ local state with no
   unresolved drift; a dead broker session is detected by the health path.

## Secret hygiene (every change)

- Broker secrets (Liberator PIN, Settrade `app_secret`/PIN) live **only** in the gitignored
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

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

## Settrade specifics (Phase 4)

### Bring-up (no overlay — cloud API)

Settrade is a **cloud Open API** — there is NO `docker-compose.settrade.yml` overlay (unlike
Liberator, which bundles `liberator-trading-api`). Credentials ride owner mode's `env_file`:

```bash
# Owner mode reads EXECUTION_ENGINE_SETTRADE_* from .env via docker-compose.private.yml:
docker compose -f docker-compose.yml -f docker-compose.private.yml up -d
```

- Engine-side config: `EXECUTION_ENGINE_SETTRADE_BASE_URL` / `_APP_ID` / `_APP_SECRET` /
  `_APP_CODE` / `_BROKER_ID` / `_PIN`. The runtime only starts when `stage ∈ {paper, micro_live,
  live}` AND owner mode AND `broker_id` + `pin` are present AND **≥1 market resolves an app trio**
  (`ACCOUNT_NO` is NOT required — the per-order account comes from `NormalizedOrder.account`).
  Missing creds log a WARNING and leave Settrade routing disabled (`micro_live` submits then 403
  `stage_rejected`).
- **Per-market broker apps (Phase 4.1).** A broker may split its books across two OAuth apps. Set
  per-market overrides — SET via `EXECUTION_ENGINE_SETTRADE_EQUITY_APP_{ID,SECRET,CODE}`, TFEX via
  `EXECUTION_ENGINE_SETTRADE_DERIVATIVES_APP_{ID,SECRET,CODE}` — and `broker_id`/`base_url`/`pin`
  stay shared. **InnovestX (broker `023`)** example: `EQUITY_*` = the `ALGO_EQ` app, `DERIVATIVES_*`
  = the `ALGO` app — this routes a stock-vs-futures spread's SET and TFEX legs concurrently. A
  market with **no** per-market override falls back to the shared `SETTRADE_APP_*` trio (the
  single-app sandbox path; UAT broker `098` is one app for both books). **Partial-trio fails loud:**
  if a per-market trio is incomplete (1–2 of the 3 fields set), that market is left UNCONFIGURED
  with a boot WARNING naming the missing field NAMES — it does **NOT** silently fall back to the
  shared app (a forgotten secret must never route a leg through the wrong app). `GET /health
  brokers.settrade.sessions` shows which markets are live.
- **UAT sandbox rehearsal:** point `EXECUTION_ENGINE_SETTRADE_BASE_URL` at
  `https://open-api-test.settrade.com` and use **`BROKER_ID=098`** (the UAT sandbox broker) to
  exercise the OAuth + order path without prod creds. The integration skeleton is
  `@pytest.mark.integration` (excluded from CI; no creds in the repo).
- **No OTP flow** (unlike Liberator): the session is OAuth app-credentials. There is no SMS
  webhook to wait on — `ensure_token()` logs in and refreshes on its own. If the breaker is
  tripped, the diagnosis is creds/clock, not a missing OTP login.
- **`micro_live`-flip prerequisite — the real trading PIN.** Reads (account/positions/heartbeat)
  work with app creds alone; **writes do not**. Before flipping a real broker (e.g. InnovestX `023`)
  to `micro_live`, confirm `EXECUTION_ENGINE_SETTRADE_PIN` holds the **real** trading PIN — the PIN
  only enters write payloads, so a configured-but-PIN-less broker reads fine yet cannot place an
  order. (Phase 4.1 validated InnovestX read-only against prod with the PIN still absent; supplying
  the real PIN is the explicit gate to the first write.)

### Secret hygiene (Settrade)

- `EXECUTION_ENGINE_SETTRADE_APP_SECRET` / `_PIN` / `_APP_ID` are `SecretStr`; the access token,
  refresh token, and the ECDSA login **signature** are redacted — never logged, never in an
  exception. Only `adapters/settrade/client.py` touches the wire.
- **The account number rides the URL path** (`/accounts/{account_no}/orders`), so the transport
  applies `redact_path` to mask it AND the third-party `httpx` logger is **demoted to WARNING at
  import** (an INFO-level httpx request log would otherwise leak the full URL incl. the account).
- **Credential rotation:** regenerate the app credentials at the Settrade **developer portal**
  (no OTP, no SMS), update `.env`, restart owner mode. `git status` must stay clean of `.env`.

### Circuit-breaker trip runbook

1. Trip signal: `GET /health` shows `brokers.settrade.breaker_state = "open"`; new
   `broker=settrade` submits return 503 `broker_circuit_open`; the trip already attempted a
   best-effort **mass-cancel** of open orders (check the engine log for the sweep result).
2. Diagnose the session: the heartbeat is an **OAuth token-liveness probe** (Settrade has no
   health endpoint), so a trip means either OAuth login/refresh is failing (creds revoked /
   expired / **clock skew** breaking the ECDSA-signed timestamp) OR the transport is down /
   timing out. Check the engine log for the login/refresh failure vs an httpx transport error.
   **Which app is dead?** With per-market apps (Phase 4.1) the heartbeat is **all-sessions** — one
   dead app trips the single breaker and mass-cancels **both** books (intended: a spread leg must
   not survive one-sided). `GET /health brokers.settrade.sessions` (`{"SET": …, "TFEX": …}`) tells
   you **which** app failed — `false` = that app's token/transport is down, `true` = healthy,
   `null` = not yet probed. Diagnose creds/clock for the `false` market's app specifically.
3. Recovery is **automatic**: the next healthy heartbeat (token acquirable AND last wire OK)
   resets the breaker (state `closed`); reconcile then repairs any drift — verify with
   `GET /orders/{client_order_id}` on anything that was in flight.
4. If the venue is healthy but you must halt anyway, use the kill-switch
   (`POST /admin/kill-switch/engage`) — it rejects new submits AND mass-cancels.

### Native-amend runbook (`PATCH /orders/{client_order_id}`)

- Settrade amends **natively** (one venue `PATCH .../change`), unlike Liberator's cancel+replace.
  The response carries the **same** `client_order_id` (a cancel_replace broker returns the
  replacement cid instead). The route is owner-mode + API-key.
- **Amend is kill-switch-gated** (unlike cancel, which is un-gated): an amend can *increase*
  exposure, so engaging the kill-switch blocks amends. PTRM re-checks the amended shape with
  **no exemption** — a price-only amend inside the duplicate-burst window can risk-reject (the
  original order stays resting, which is safe).
- A **venue amend-reject** (e.g. a partial fill raced the amend) is NON-terminal: the order is
  **still live** and a typed `AmendRejected` (409) is returned. `reject_reason` is deliberately
  NOT written — the **two audit rows** (`PENDING_REPLACE` then the restore to `NEW`/
  `PARTIALLY_FILLED`) are the durable evidence. Check `execution.order_events` for the cid, not
  `reject_reason`.
- A **stranded `PENDING_REPLACE`** (process crash or lost response mid-amend) is repaired by the
  reconciler's `replace_resolve` action (venue-truth price/qty restore). Mass-cancel **skips
  `PENDING_REPLACE` rows by construction** (no frozen cancel edge from that state) — so after a
  kill-switch engage, a stranded amend is NOT swept by the first sweep: **re-run engage (or wait
  one reconcile pass)** so `replace_resolve` returns the row to `NEW`/`PARTIALLY_FILLED` first,
  then the next sweep cancels it.

### Stage-flip rule (unchanged)

The Liberator stage-flip rule applies verbatim: cancels resolve their adapter from the CURRENT
stage, so **before changing `EXECUTION_ENGINE_STAGE` while `broker=settrade` orders are open,
engage the kill-switch (mass-cancels everything), verify `GET /health`, then flip and
disengage.** Do not rely on the reconciler to fix mis-routed cancels.

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

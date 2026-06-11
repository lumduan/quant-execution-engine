# Phase 4.1: Settrade per-market broker apps — SET + TFEX concurrently (spread-trade ready)

> **Status:** Complete (2026-06-11)
> **Branch:** `feature/phase4.1-settrade-per-market-apps`
> **Parent plan:** [`ROADMAP.md`](ROADMAP.md) — "Phase 4.1 — Settrade per-market broker apps"
> **Builds on:** [`phase4-settrade-adapter.md`](phase4-settrade-adapter.md)

## Table of Contents

- [Overview](#overview)
- [AI Prompt](#ai-prompt)
- [Scope](#scope)
- [Design Decisions](#design-decisions)
- [Implementation Steps](#implementation-steps)
- [File Changes](#file-changes)
- [Success Criteria](#success-criteria)
- [Completion Notes](#completion-notes)

## Overview

### Purpose

Phase 4 shipped `SettradeAdapter` with **one** OAuth client serving both markets — correct for
the UAT sandbox (broker `098`, a single app), but the real broker **InnovestX (broker 023)
splits the two markets across two OAuth apps**: `ALGO_EQ` (SET equity) and `ALGO` (TFEX
derivatives), each with its own `app_id`/`app_secret`/`app_code`. One app can therefore never
route both legs of a stock-vs-futures spread. The operator trades **stock-vs-futures spreads**
and needs `broker=settrade` to route the SET and TFEX legs **concurrently**, each through its
own app.

Phase 4.1 is a **focused in-engine refactor**: the adapter now holds **one `SettradeClient` per
market** behind the unchanged `NormalizedOrder` contract — "like Liberator" in the sense that
both markets sit behind one adapter (Liberator gets that for free from its single-credential
upstream). The UAT-sandbox single-app configuration keeps working unchanged (the shared trio
resolves to one client under both market keys — one login, one session).

### Key Deliverables

1. Six new per-market settings (`EXECUTION_ENGINE_SETTRADE_{EQUITY,DERIVATIVES}_APP_{ID,SECRET,CODE}`)
   and a pure per-market credentials-resolution rule in `adapters/settrade/runtime.py`.
2. `SettradeAdapter` ctor takes `clients: Mapping[Market, SettradeClient]`; `place`/`cancel`/`amend`
   resolve the per-market client first; an unconfigured market returns a typed not-ok ack and
   reads skip it.
3. New `SettradeMarketNotConfigured` error: `fetch_venue_orders` **raises** (never `[]`) for an
   unconfigured market, so the reconciler freezes affected rows instead of forging venue truth.
4. All-sessions heartbeat on the single frozen breaker (one dead app trips it and mass-cancels
   **both** books); per-market reconciler GET-budget skip set; additive `/health`
   `brokers.settrade.sessions` field.
5. Real-venue read-only verification against InnovestX (broker `023`) prod through the refactored
   adapter (both apps' tokens acquired; equity + TFEX reads routed each through its own client).

### Parent Plan Reference

- [`docs/plans/ROADMAP.md`](ROADMAP.md) — "Phase 4.1 — Settrade per-market broker apps".
- [`phase4-settrade-adapter.md`](phase4-settrade-adapter.md) — the Phase 4 adapter this extends.
- Frozen contracts (unchanged): [`normalized-order-contract.md`](../../.claude/knowledge/normalized-order-contract.md),
  [`order-state-machine.md`](../../.claude/knowledge/order-state-machine.md),
  [`capability-matrix.md`](../../.claude/knowledge/capability-matrix.md) (capability **cells**
  unchanged — only the auth-model note gains the per-market split),
  [`broker-research-settrade.md`](../../.claude/knowledge/broker-research-settrade.md),
  [`decision-log.md`](../../.claude/knowledge/decision-log.md) (E28).

## AI Prompt

The binding request that initiated this phase is reproduced **verbatim** below. Two scope
decisions were confirmed with the operator during planning and are reflected throughout this
document (see Design Decisions): the refactor is **in-engine, two clients behind one adapter**
(NOT a new `third_party` service), and there is **no batch / multi-leg endpoint** (a spread is
two independent `POST /orders`, one per leg; the spread logic stays in the strategy).

```text
yes, InnovestX is correct data
but i some time i want send data SET and TFEX together for spread trade Futures and Stocks , I want you refactor it support SET , TFEX like liberator api in 3rd party
plan before implement

[follow-up message during planning:]
when plan done please update this phase plan and roadmap too
```

## Scope

### In Scope

| # | Deliverable | Status |
|---|---|---|
| 1 | 6 per-market settings (`settrade_{equity,derivatives}_app_{id,secret,code}`; creds `SecretStr`, code `str`, all optional) | Done |
| 2 | Per-market independent credentials resolution (complete per-market trio wins; PARTIAL trio = market unconfigured + WARNING naming missing field NAMES, no silent shared fallback; shared trio = single-app path; mixed mode allowed) | Done |
| 3 | Client dedupe by credentials value (markets resolving to equal trios share ONE `SettradeClient` — sandbox ⇒ one login under both keys) | Done |
| 4 | `SettradeAdapter` ctor `clients: Mapping[Market, SettradeClient]`; `place`/`cancel`/`amend` resolve the client first → typed not-ok ack for an unconfigured market | Done |
| 5 | `SettradeMarketNotConfigured` (`errors.py`); `fetch_venue_orders` raises it for an unconfigured market (never `[]`); reads skip unconfigured markets | Done |
| 6 | All-sessions heartbeat (probe all distinct sessions, id-deduped; healthy ⇔ every configured app alive) on the single frozen breaker; one dead app trips + mass-cancels both books | Done |
| 7 | `get_budget_exhausted(market)` per-market; reconciler per-market budget skip set (`exhausted: set[Market]`) | Done |
| 8 | Additive `/health` field `brokers.settrade.sessions = {"SET": bool\|None, "TFEX": bool\|None}` | Done |
| 9 | `aclose` id-deduped (each distinct client closed once) | Done |
| 10 | Tests (dispatch matrix, hook renames, partial-trio matrix, sandbox single-login regression) + UAT integration: opt-in InnovestX per-market read-only | Done |

### Out of Scope (deferred / explicitly not done)

- **A batch / multi-leg / spread endpoint.** A spread is two independent `POST /orders` (one per
  leg); the engine routes each leg, the strategy owns the pairing (operator-confirmed).
- **A `third_party` Settrade service.** This is an in-engine two-client refactor — there is no new
  composed upstream (Settrade is a cloud API; Liberator's "both markets behind one adapter" comes
  from its single-credential upstream, not from a service we add).
- **Per-market circuit breakers.** The Phase-0 `BrokerAdapter` base owns exactly ONE breaker per
  adapter (frozen); per-market breakers are not expressible (see Design Decision 3).
- **Capability-cell changes.** The SET + TFEX capability cells (order types, TIFs, amend
  semantics) are untouched — only the auth-model note records the per-market split.
- **Touching the wire.** `client.py`, `mapping.py`, `models.py`, `core/`, `db/`, `contracts/`,
  the PATCH route, `NormalizedOrder`, and `POST /orders` are all unchanged.

## Design Decisions

### 1. Effective credentials per market — independent, with partial-trio-fails-loud

A market's trio is `(app_id, app_secret, app_code)`, resolved **per market and independently**:

- **Per-market trio complete** (SET → `settrade_equity_*`, TFEX → `settrade_derivatives_*`) → use it.
- **Per-market trio PARTIAL** (1–2 fields present) → the market is **UNCONFIGURED** with a boot
  **WARNING naming the missing field NAMES** — a **loud** failure, with **no silent fallback** to
  the shared trio. A forgotten secret must never route equity through the wrong app.
- **Else** the shared `settrade_app_*` trio complete → use it (the sandbox single-app path).
- **Else** the market is unconfigured. **Mixed mode** (one market per-market, the other shared) is
  allowed. `broker_id`/`base_url`/`pin`/intervals stay shared across both markets.

Enabled = `stage ∈ {paper, micro_live, live}` ∧ owner mode ∧ `broker_id`+`pin` present ∧ ≥1 market
resolves; a WARNING fires when exactly one market is configured (the other's orders will be
rejected). `_effective_credentials` / `_configured_markets` are pure functions — the test surface.

*Rejected:* silent fallback to the shared trio on a partial per-market trio (a forgotten secret
would route a leg through the wrong app — the exact foot-gun this rule exists to prevent).

### 2. Clients deduped by credentials value

Markets resolving to **equal** trios share **one** `SettradeClient` instance. `SettradeAppCredentials`
is a `frozen` dataclass keyed in a `by_creds` dedupe map (`SecretStr` is hashable + value-equal,
verified). The UAT-sandbox single-app path therefore keys **one** client under both market keys ⇒
one login, one session, exactly as before Phase 4.1. The adapter further dedupes by `id()` for
heartbeat probing and `aclose` so the shared instance is probed/closed exactly once.

### 3. All-sessions heartbeat on the single frozen breaker (E28)

The `BrokerAdapter` base owns **exactly one** breaker per adapter (frozen in Phase 0) — per-market
breakers would thaw that invariant and are not expressible. So the heartbeat probes **all** distinct
sessions and is healthy **only when every configured app is alive**; one dead app trips the single
breaker and **mass-cancels both books**.

This is the conservative spread semantics, not a limitation: a spread holds **one leg per app**, so
if one app dies the other leg is un-hedgeable — routing the surviving market would *increase*
one-sided exposure. Tripping and flattening both books is correct. `/health`
`brokers.settrade.sessions` shows **which** app died for diagnosis.

### 4. Unconfigured `fetch_venue_orders` raises — never returns `[]`

For an unconfigured market the reconciler-facing `fetch_venue_orders` **raises**
`SettradeMarketNotConfigured`, never an empty list. An empty list would forge "venue says zero
orders" and drive `cancel_confirm` / `ack_lost` transitions against possibly-live rows. The
reconciler treats the raise as a group-skip: the affected rows **freeze** and a WARNING nags,
rather than transition on fabricated truth.

*Rejected:* returning `[]` for an unconfigured market (it is indistinguishable from a real
empty book and silently drives terminal transitions).

### 5. Per-market reconciler budget skip set

`reconcile_once` replaces Phase 4's whole-pass budget `break` with an `exhausted: set[Market]`
skip set: one market's exhausted GET bucket skips only **that** market's `(account, market)`
groups; the other market's groups still poll. The old whole-pass break inverted starvation — a
single starved bucket would stall the healthy client's groups too. One WARNING per exhausted market
per pass. `get_budget_exhausted(market)` is now per-market (signature change); an unconfigured
market is never exhausted (its fetch raises on the one code path).

### 6. Additive `sessions` health field

`BrokerRuntimeHealth` gains an **additive** `sessions: dict[str, bool | None] | None = None`; the
`/health` settrade entry passes `sessions={m.value: ok for m, ok in
adapter.last_heartbeat_by_market.items()}`. The existing `session_healthy` aggregate
(`last_heartbeat_ok`) is unchanged; the Liberator entry is untouched (its `sessions` stays `None`).
The field reports `{"SET": bool|None, "TFEX": bool|None}` — `None` before the first heartbeat.

### 7. Place resolves the client first (before building any payload)

`place` resolves the per-market client **before** `mapping.to_place_payload` — an unroutable order
must never serialize the PIN into a payload it cannot send. An unconfigured market returns
`PlaceAck(rejected=True, reject_reason="settrade: no <MARKET> broker app configured")`; `cancel` and
`amend` mirror with `ok=False` carrying the same reason (`amend` keeps `semantics="native"`).

## Implementation Steps

1. [x] `config/settings.py`: 6 per-market fields after the shared trio; comment documents the
   per-market override + partial-trio-fails-loud rule (`broker_id`/`base_url`/`pin`/intervals stay
   shared).
2. [x] `adapters/settrade/runtime.py`: `SettradeAppCredentials` frozen dataclass;
   `_effective_credentials` / `_configured_markets` pure resolution; `_secrets_present` =
   `broker_id ∧ pin ∧ bool(_configured_markets(...))`; `create_settrade_runtime` builds
   `clients: dict[Market, SettradeClient]` via a `by_creds` dedupe map, passes `clients=`; INFO log
   naming configured markets + source (`SET:per-market,TFEX:shared`), never secrets.
3. [x] `adapters/settrade/errors.py`: `SettradeMarketNotConfigured` (shares the settrade base).
4. [x] `adapters/settrade/adapter.py`: ctor `clients: Mapping[Market, SettradeClient]` (+ guard);
   `_client_for` / `_distinct_clients` / `_no_app_reason`; `place`/`cancel`/`amend` resolve the
   client first → typed not-ok acks; `fetch_venue_orders` raises for an unconfigured market; reads
   skip unconfigured markets; `get_budget_exhausted(market)`; all-sessions `heartbeat` filling
   `last_heartbeat_by_market`; id-deduped `aclose`.
5. [x] `adapters/settrade/reconciler.py`: per-market `exhausted` skip set; `except (…,
   SettradeMarketNotConfigured)` group-skip.
6. [x] `api/schemas.py`: additive `BrokerRuntimeHealth.sessions`. `api/routes.py`: settrade entry
   passes `sessions=...` (liberator untouched).
7. [x] Tests: dispatch matrix (SET→equity client, TFEX→derivatives client, unconfigured →
   rejected ack + zero HTTP, sandbox SET+TFEX = ONE login), hook renames (`adapter._client` →
   `adapter._clients[Market.SET]`), heartbeat dual one-dead-trips, per-market budget skip,
   resolution matrix, `/health sessions`, UAT per-market read-only integration.
8. [x] Quality gate green; real-venue read-only verification through the refactored adapter; docs
   (this plan + ROADMAP + E28 + capability/broker-research notes + playbook + CLAUDE.md).

## File Changes

| File | Action | Description |
|---|---|---|
| `docs/plans/phase4.1-settrade-per-market-apps.md` | add | This plan |
| `src/quant_execution_engine/config/settings.py` | modify | 6 per-market `settrade_{equity,derivatives}_app_{id,secret,code}` fields + resolution comment |
| `src/quant_execution_engine/adapters/settrade/runtime.py` | modify | `SettradeAppCredentials`; `_effective_credentials`/`_configured_markets`; `by_creds` client dedupe; `clients=` ctor; per-market boot log |
| `src/quant_execution_engine/adapters/settrade/errors.py` | modify | `SettradeMarketNotConfigured` |
| `src/quant_execution_engine/adapters/settrade/adapter.py` | modify | `clients` mapping ctor; per-market client resolution; raise-not-`[]` fetch; all-sessions heartbeat; per-market budget; id-deduped aclose |
| `src/quant_execution_engine/adapters/settrade/reconciler.py` | modify | per-market `exhausted` skip set; `SettradeMarketNotConfigured` group-skip |
| `src/quant_execution_engine/api/schemas.py` | modify | additive `BrokerRuntimeHealth.sessions` |
| `src/quant_execution_engine/api/routes.py` | modify | settrade `/health` entry passes `sessions=` |
| `tests/unit/adapters/settrade/test_*.py`, `tests/test_core_router_settrade.py`, `tests/integration/.../test_live_settrade_uat.py` | modify | dispatch matrix + hook renames + resolution matrix + per-market budget + `/health sessions` + UAT per-market read-only |
| `docs/plans/ROADMAP.md`, `docs/plans/phase4-settrade-adapter.md`, `.claude/knowledge/{decision-log,capability-matrix,broker-research-settrade}.md`, `.claude/playbooks/order-routing-safety.md`, `CLAUDE.md` | modify | Phase 4.1 status, E28, per-market notes, playbook, current-state |

## Success Criteria

- [x] The **same** `NormalizedOrder` routes SET via the equity app and TFEX via the derivatives
  app **concurrently** — SET place hits only the equity client (equity login called, derivatives
  not, Authorization carries the equity token); TFEX hits only the derivatives client.
- [x] An unconfigured market returns a typed not-ok ack (`place`/`cancel`/`amend`) with **zero**
  HTTP; `fetch_venue_orders` raises `SettradeMarketNotConfigured` (never `[]`); reads skip it.
- [x] Partial per-market trio → that market UNCONFIGURED with a boot WARNING naming the missing
  fields, **no** silent shared fallback (resolution matrix test); mixed mode works.
- [x] Sandbox single-app regression: SET + TFEX both route through ONE `SettradeClient` instance
  (one login recorded; `aclose` closes it once).
- [x] All-sessions heartbeat: equity-ok + derivatives-dead → `False`,
  `last_heartbeat_by_market == {SET: True, TFEX: False}`, the single breaker trips, mass-cancel
  fires once; `/health brokers.settrade.sessions` reports which app is dead.
- [x] Per-market reconciler budget: SET bucket exhausted on a dual adapter → SET groups skipped,
  TFEX group still polled; sandbox single-client exhaustion skips all (regression).
- [x] Quality gate green: ruff + ruff format + mypy strict + **pytest 713 passed, 96.22%
  coverage**.
- [x] Real-venue read-only verification through the refactored adapter (see Completion Notes).

## Completion Notes

### Summary

Landed on `feature/phase4.1-settrade-per-market-apps` (2026-06-11), gate-green. A focused in-engine
refactor: `SettradeAdapter` now holds **one `SettradeClient` per market** behind the unchanged
`NormalizedOrder` contract, so `broker=settrade` routes SET via the equity app and TFEX via the
derivatives app **concurrently** — spread-trade ready. The UAT-sandbox single-app path is
unchanged (the shared trio resolves to one client under both market keys via the `by_creds`
dedupe). Per-market credentials resolve independently with **partial-trio-fails-loud** (no silent
shared fallback); an unconfigured market returns typed not-ok acks, reads skip it, and
`fetch_venue_orders` **raises** `SettradeMarketNotConfigured` rather than forge an empty venue book.
The all-sessions heartbeat keeps the single frozen Phase-0 breaker (E28): one dead app trips it and
mass-cancels **both** books (spread legs must not survive one-sided). The reconciler's per-market
budget skip set stops a starved bucket from stalling the healthy client's groups. `/health` gains
an additive `brokers.settrade.sessions` field showing which app is alive.

Nothing on the wire or contract changed: `client.py`, `mapping.py`, `models.py`, `core/`, `db/`,
`contracts/` (capability **cells** unchanged), the PATCH route, `NormalizedOrder`, and `POST /orders`
are all untouched. A spread remains two independent submits (no batch endpoint), and this is an
in-engine refactor (no new `third_party` service) — both operator-confirmed.

**Final gate:** ruff + ruff format + mypy strict all clean; **pytest 713 passed**; **total coverage
96.22%**. `adapters/settrade/adapter.py` is **439 lines** (over the ≤400-line target, by design —
the per-market routing, all-sessions heartbeat, and read fan-out are one cohesive adapter concern;
splitting them would scatter the routing logic).

### Real-venue verification

The opt-in InnovestX per-market read-only integration test ran against the **prod broker (023)**:
both OAuth apps' tokens were acquired, the **equity account `902001825`** was read through the
`ALGO_EQ` (SET) client, and **TFEX `507619-0`** through the `ALGO` (derivatives) client. No writes;
the PIN was never serialized (reads never send it).

### Micro_live prerequisite (explicit gap)

The InnovestX **trading PIN is still NOT in `.env`** (`EXECUTION_ENGINE_SETTRADE_PIN`). Reads work
without it — the PIN only enters write payloads — so it is the explicit **prerequisite for flipping
InnovestX to `micro_live`**. Until the real PIN is present, writes are not possible even with both
apps configured. STAGE stays `sim` + public mode (routing disabled); stage flips remain an explicit
operator step (see the safety playbook's Settrade runbook).

---

**Document Version:** 1.0
**Status:** Complete (2026-06-11)

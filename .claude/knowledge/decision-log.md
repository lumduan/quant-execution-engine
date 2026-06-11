# Decision log — quant-execution-engine

> The cross-cutting Decision Log **D1–D13** is authored in the umbrella roadmap
> ([`plans/feature-execution-engine/ROADMAP.md`](../../../plans/feature-execution-engine/ROADMAP.md))
> and pinned by the ADR
> ([`.claude/knowledge/feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md)).
> **Phase 0 confirmed all thirteen as drafted — ACCEPTED 2026-06-10; the ADR is the source of
> truth.** This file records the summary, the per-service decisions, the broker-research
> findings, and the Phase 0 pinned resolutions.

## Accepted (D1–D13, summary — confirmed in the Phase 0 ADR, 2026-06-10)

| # | Decision |
|---|---|
| D1 | Execution and streaming are **separate planes/services**; this is execution-only. |
| D2 | **Standalone `quant-execution-engine`** (host `:8400`), gateway-proxied — not a gateway module. |
| D3 | **`BrokerAdapter` interface** (`place/cancel/amend/get_open_orders/get_positions/get_account/capabilities`). |
| D4 | **`NormalizedOrder` contract** + status enum `NEW\|PARTIALLY_FILLED\|FILLED\|CANCELLED\|REJECTED\|EXPIRED`. |
| D5 | **Client-generated `client_order_id`** idempotency key; dedupe before routing. |
| D6 | **Durable order store in `quant-infra-db`** — `execution.orders` + `fills` + append-only `order_events`. |
| D7 | **Per-adapter capability matrix**; router rejects unsupported combos up front. |
| D8 | **Reconciliation loop** repairs broker-truth ↔ local-state drift. |
| D9 | **`LiberatorAdapter` wraps the existing `liberator-trading-api`** over HTTP; does not re-implement it. |
| D10 | **Auth/session stays inside each adapter** (Liberator OTP vs Settrade OAuth). |
| D11 | **`SimAdapter` + global kill-switch + pre-trade risk gate from day one.** |
| D12 | **Normalized order-update stream out** (WS/events). |
| D13 | **Strategies submit behind a flag** (`*_EXECUTION_MODE = off\|sim\|live`, default off/sim). |

## Per-service decisions (this repo)

| # | Decision | Rationale |
|---|---|---|
| E1 | **Env prefix `EXECUTION_ENGINE_*`**, `pydantic-settings`. | Matches the umbrella per-service convention; no secret in repo. |
| E2 | **`EXECUTION_ENGINE_STAGE = sim\|paper\|micro_live\|live`, default `sim`.** | The execution analogue of the tfex capital ladder + marketdata `mirror\|engine` cutover — gate the dangerous rung, default the safe one. |
| E3 | **Public mode disables order-submission endpoints** (Docker default `true`). | Mirrors the marketdata-engine public/owner split; submits are owner-mode only. |
| E4 | **Own Redis sidecar** (`quant-execution-redis`), internal-only. | Dedupe / single-flight submit lock / rate-limit, distinct from the gateway's Redis (D2 isolation). |
| E5 | **`db_execution` Postgres database** for the `execution.*` schema. | Per-engine DB boundary, like `db_market_data`. |
| E6 | **Scaffold ships `/health` only**, ≥90% coverage gate from day one. | Safe skeleton; no order path can run until Phase 2 wires the state machine + risk gate. |

## Findings resolved by the broker research (2026-06-09)

| # | Finding | Consequence |
|---|---|---|
| R1 | **Neither Liberator nor Settrade accepts a client idempotency key** (broker order_no only). | The engine owns the `client_order_id ↔ broker_order_id` mapping; dedupe + reconcile is how we approximate exactly-once (D5/D8). Pin "at-least-once + dedupe", not "exactly-once", in Phase 0. |
| R2 | **Liberator has no amend route; Settrade has native `change_order`.** | `BrokerAdapter.amend` is uniform; `LiberatorAdapter.amend` = cancel+replace (declared non-atomic). Capability divergence enforced by the router (D7). |
| R3 | **Settrade exposes a native order-update push** (`subscribe_derivatives_order`); Liberator does not. | Phase-5 stream: Settrade feeds it directly; Liberator state is reconciled/poll-normalized into the same shape (D12). |
| R4 | **Settrade `price_type`/`validity_type`/`trigger_session` enum sets are SDK-passthrough strings.** | The exact venue enums are pinned in Phase 4 against the live venue, not guessed in the contract — `(confirm P4)` cells in the capability matrix. |
| R5 | **Both brokers take a per-order PIN; auth differs** (Liberator OTP/SMS + Redis token, Settrade OAuth auto-refresh + rate-limit). | Session liveness is adapter-local (D10); the health/reconcile path must detect a dead session before it silently drops orders. |

## Phase 0 pinned resolutions (2026-06-10)

The ROADMAP's "Open questions / risks" were resolved as written decisions in the ADR
(Pinned **§A–§G**); owner stances + parameters confirmed 2026-06-10. One-line summaries —
the ADR text governs:

| § | Resolution |
|---|---|
| §A | Delivery guarantee = **at-least-once + dedupe + reconcile + idempotent re-submit, NOT exactly-once** (R1). `client_order_id` standard: **UUIDv4**, client-generated, opaque to the engine (time-ordered drop-ins acceptable; the id is never parsed for time). |
| §B | `client_order_id ↔ broker_order_id` persisted **atomically with `PENDING_NEW → NEW`**; lost-ack fallback: fuzzy match `(account, symbol, side, qty)` within **±5 s** of the persisted submit ts; a stuck `PENDING_NEW` resolves bounded, never blocks routing indefinitely. |
| §C | `NormalizedOrder` / `NormalizedOrderResult` / `NormalizedStatus` **frozen** — `Decimal`-as-string wire, `int` qty, UTC. |
| §D | `BrokerAdapter` 7-method interface frozen; amend semantics declared per adapter. |
| §E | Order state machine frozen — 9 states + complete legal-transition table. |
| §F | Capability-matrix shape frozen — per-`(broker, market)` sets, router-enforced pre-venue; Liberator amend = cancel+replace **non-atomic** (queue-loss declared); `(confirm P4)` enums deferred-by-design (R4). |
| §G | Order-type validation = distinct pre-flight class per `(broker, market, order_type)` (Phase 3/4); auth liveness = **~30 s heartbeat + circuit breaker** per adapter; blast radius = PTRM caps + kill-switch **(reject new + mass-cancel open)** as Phase-2 milestones; streaming = external read-only dependency (D1 reaffirmed). |

## Phase 2 realisation decisions (E7–E12, 2026-06-10)

| # | Decision | Rationale |
|---|---|---|
| E7 | **Synchronous in-request sim fills**; `repositories.apply_fill()` is the standalone seam Phase-3/4 stream/reconcile workers reuse. | Deterministic acceptance from one POST; no background ordering nondeterminism for an in-proc adapter. |
| E8 | **Additive `engine_state` Result field** (internal 9-state truth) beside the frozen 6-value `status`. | Keeps the frozen enum intact while keeping the §B reconciliation window operator-visible. Contract addendum, not a change. |
| E9 | **Kill-switch precedes even dedupe** in the submit path; cancels are NOT kill-switch-blocked (mass-cancel uses the cancel path). | Hard rule 3 ("checked first") wins over the validation-list ordering; cancels reduce risk. |
| E10 | **Runtime kill-switch trip = Redis key + admin endpoints** (engine-direct, owner-mode, never proxied); env flag is the boot-time backstop and pins over runtime disengage. | An env-only switch needs a restart, during which nothing mass-cancels. |
| E11 | **Single-flight submit lock is politeness; the orders PK is correctness.** Lock-miss ⇒ brief store-poll ⇒ 200 duplicate or 409 `submit_in_flight`; Redis-down ⇒ PK arbitrates. PTRM rate/burst fail-open in `sim|paper`, fail-closed in `micro_live|live`. | At-least-once + dedupe (§A) holds with or without Redis. |
| E12 | **`metadata` is the sim control channel** (`sim_fills`, `sim_reject`) — never venue-sent by any adapter, never persisted. `reject_reason` persists durably (Phase-2 column); the audit trigger runs with INVOKER rights so the service role holds INSERT on `order_events` (append-only stays trigger-enforced). | Deterministic lifecycle steering without contract surface; real-money audit must not live in a cache. |

## Phase 3 realisation decisions (E13–E20, 2026-06-11)

| # | Decision | Rationale |
|---|---|---|
| E13 | **Liberator runtime is a process singleton** (`adapters/liberator/runtime.py`, the `db/postgres.py` pattern): adapter + breaker + httpx client + workers live module-level; `api/deps.py` injects it into the per-request `OrderRouter` (optional param, `None` = unconfigured). | Routers are built per request — breaker/heartbeat state must outlive them; existing constructor call sites stay valid. |
| E14 | **Stage matrix via `AdapterIntent` (`TRADE\|READ`)**: `sim` routes every broker to sim (sim is a *stage*, not a broker — the prompt's "reject liberator at sim" resolved this way to preserve Phase-2 semantics); `paper` intercepts TRADE to sim while READ reaches the live Liberator session; `micro_live` routes `broker=liberator` real (typed reject when unconfigured) and rejects other brokers; `live` stays gated. | One axis expresses the paper place-intercept + read-realism requirement without a second adapter seam. |
| E15 | **Reconciler runs only at `micro_live`/`live`** (heartbeat runs from `paper` up). This deliberately narrows the prompt's "not in sim stage" wording. | At `paper`, placements land in sim — "reconciling" sim-acked `broker=liberator` rows against venue truth would corrupt them. |
| E16 | **`CircuitOpenError.code` renamed `broker_session_down` → `broker_circuit_open`** (Phase 3 spec pins the wire code; 503 mapping kept). | One condition, one code; nothing consumed the Phase-2 placeholder string. |
| E17 | **Amend = router-level cancel+replace through the frozen PENDING_CANCEL path** with a caller-supplied fresh `client_order_id`; `PENDING_REPLACE` stays reserved for native amends (Settrade, Phase 4 — no `PENDING_REPLACE→CANCELLED` edge exists). The replacement runs the FULL submit pipeline with **no PTRM exemption**: a same-qty price-only amend inside the duplicate-burst window risk-rejects flat (old order already cancelled, never doubled). `LiberatorAdapter.amend()` itself returns a declared-failure `cancel_replace` ack — it never fakes atomicity. No amend HTTP route until Phase 4. | Persistence is repository work, not adapter work; the frozen graph and the frozen pipeline both stay intact. |
| E18 | **Reconciler fills are cumulative-watermark deltas**: `broker_fill_id = f"{orderNo}:{matched}"` (re-polls dedupe via the existing `ON CONFLICT DO NOTHING`); fill price = venue order price, falling back to `amount/matched`, then the local order price — a documented approximation until a per-fill stream exists (Phase 5). Fills seen while `PENDING_CANCEL` are surfaced (WARNING), not persisted — no legal edge. | Liberator reports no per-fill records; determinism + idempotency beat invented granularity. |
| E19 | **Heartbeat target = `GET order/health/set`**: healthy ⇔ HTTP 200 ∧ `status=="healthy"` ∧ `auth_token_available` — the last term is exactly the dead-OTP-session signal; no venue round-trip, no PIN. `/session/status` rejected (fires a real venue probe — too heavy at 30 s). | ADR §G wants low-impact; a 200 with no auth token must still count as session-dead. |
| E20 | **Venue states with no frozen edge map to the nearest truthful terminal**: venue-cancelled without local PENDING_CANCEL → two-step PENDING_CANCEL→CANCELLED; post-ack venue reject → `reject_reason` persisted + close-out CANCELLED + WARNING (no `NEW→REJECTED` edge); lost-ack PENDING_NEW unmatched past 60 s (~5 passes) → `REJECTED "ack_lost_unmatched"`, never re-sent; ambiguous fuzzy matches are skipped, never guessed. | Frozen graph governs; venue truth is preserved in `reject_reason` even when the status word can't be. |

**Upstream hardening (dual-commit, 2026-06-11):** the user-requested verification-system audit
of `liberator-trading-api` found a timing-unsafe `==` API-key comparison, `verify_api_key`
copy-pasted across 14 endpoint modules (with `app/dependencies.py` empty), per-request
`os.getenv("API_KEY")` (500 when unset), and naive `datetime.now()` mislabeled with a `Z`
suffix. Fixed upstream (own repo, then pinned here): one shared timing-safe
(`hmac.compare_digest`) `verify_api_key` dependency resolving the key from settings and
failing closed (503), endpoints import it, health timestamps are real UTC. Wire contract
unchanged (401 missing / 403 invalid).

## Phase 4 realisation decisions (E21–E27, 2026-06-11)

| # | Decision | Rationale |
|---|---|---|
| E21 | **Raw `httpx.AsyncClient`, NOT the `settrade-v2` SDK.** The wire is re-implemented directly; shapes pinned from the SDK v2.2.1 source cross-checked against the scraped official venue docs (see E26). One new dep: `cryptography>=42` for ECDSA P-256 login signing. | The SDK is sync `requests` (forbidden in `src/` — blocks the loop) with import-time side effects unacceptable in a service: it writes `~/settradesdkv2_config.txt`, makes an NTP call, and fires a version-check HTTP request on import. Thread-pooling or vendoring it keeps those. |
| E22 | **Native amend orchestration.** Kill-switch gates amends **up front** (an amend can increase exposure — a documented asymmetry vs the un-gated cancel path); PTRM re-checks the hypothetical amended order with **NO exemption**; the commit is one `replace_order` UPDATE setting status+price+quantity atomically (`PENDING_REPLACE → NEW`) so the audit row snapshots the amended values; a partially-filled order does a two-step restore (`cancel_two_step` precedent → `NEW`, then `PARTIALLY_FILLED`). A venue amend-reject is a **NON-terminal restore** + typed `AmendRejected` (409, wire code `amend_rejected`) — the order is still live, so `reject_reason` is deliberately untouched (the two audit rows are the durable evidence). | The frozen 13-edge machine reserves `PENDING_REPLACE → NEW` for native amends with the price/qty columns unconstrained by the guard; a two-statement update would snapshot a stale price; writing `reject_reason` would imply the order is dead. No ADR/edge change, no infra-db migration — verified against the live trigger. |
| E23 | **Heartbeat = OAuth token-liveness probe** (vs E19's real venue health route). Healthy ⇔ `ensure_token()` succeeds AND `last_wire_ok is not False`; N consecutive failures (default 3) trip → `broker_circuit_open` + mass-cancel; healthy probe resets. The residual blind spot — a valid token whose order endpoints are degraded — is documented and fixed in Phase 5 (MQTT gives a real session signal). | Settrade exposes **no** health/session endpoint (Liberator has one — E19). Token acquirability IS the OAuth session; waiting for a 401 on a live order is unacceptable (ADR §G). |
| E24 | **Reconciler mirrors Liberator (mirror-not-abstract); extraction to a shared base deferred to Phase 6.** Fills are cumulative-watermark deltas (E18): `broker_fill_id = f"{order_no}:{matched}"`, ON-CONFLICT-dedupe; §B constants verbatim (5 s stuck, 60 s `ack_lost_unmatched`, ±5 s fuzzy, unique-candidate-only); grouped per `(account, market)`. **New action `replace_resolve`** repairs a stranded `PENDING_REPLACE` (crash/lost response mid-amend): venue resting → `replace_order(venue price/qty)`; venue terminal → resolve then legal close-out; venue missing → restore local values. `fetch_orders_for_reconcile(include_pending_replace=True)`. | A verbatim structural mirror is the lower-risk move for a second adapter; premature abstraction over two examples buys nothing. Native amend introduces a new stranded local state the §B watermark machinery doesn't cover. |
| E25 | **Rate limits = observe-don't-throttle (v1).** The client parses `X-RateLimit-*` into a `rate_snapshot()`; the adapter exposes `get_budget_exhausted()` (the reconciler does NOT reach into the client's private rate state); the reconciler **budget-skips** remaining `(account, market)` groups when the GET bucket is exhausted; a zero-remaining read logs a WARNING. GET and POST+PATCH live in **separate buckets** so reconcile reads never starve order writes. Adaptive submit-path throttling is Phase 6. | Observe-first is the safe default for a real-money submit path; throttling the writes risks masking a real backlog. The `get_budget_exhausted()` seam keeps the reconciler off the client's internals. |
| E26 | **SET equity scope = operator amendment; conservative cells replaced by doc-pinned cells.** The Phase-0/old-ROADMAP "no SET on Settrade (derivatives first)" non-goal was **struck by operator decision** — Phase 4 ships 100% of the settrade-v2 INVESTOR order surface (SET + TFEX). The former `(confirm P4)` enum cells were pinned from the **official venue docs**, discovered to be served as raw markdown by the `developer.settrade.com/template/open-api/...` backend (menu `config.json` + `{n}_{name}.md` pages) — superseding the Phase-3-era "the SPA cannot be scraped" note. | The SPA renders its content from a scrapable markdown backend; that discovery turned the enum-pinning fallback (guess conservatively, validate at micro_live) into verified cells before any code shipped. Recipe recorded in `broker-research-settrade.md` as the doc-pinning vehicle for future phases. |
| E27 | **`wire_price` float-exactness (reject, never re-quantize)** + `price=0` rule + stop-condition side derivation. `Decimal → float` only when the float's `repr` round-trips exactly back to the `Decimal`, else a typed reject. `ATO`/`ATC`/`MP-MTL`/`MP-MKT` (and the `STOP` market leg) send `price: 0`. Stop side: `BUY → 'LAST_PAID_OR_HIGHER'`, `SELL → 'LAST_PAID_OR_LOWER'`, `stopSymbol = order.symbol`. | Money is `Decimal` at the boundary (umbrella rule); the venue wire takes JSON numbers, so the float crossing must be provably lossless or rejected. The contract has `stop_price` but no condition field — the side derives the only safe v1 condition; `SESSION`/explicit conditions stay out of scope (E26). |

## Phase 4.1 realisation decisions (2026-06-11)

| # | Decision | Rationale |
|---|---|---|
| E28 | **Settrade per-market broker apps — one `SettradeClient` per market behind the unchanged adapter.** The real broker **InnovestX (023)** splits the books across two OAuth apps (`ALGO_EQ` = SET equity, `ALGO` = TFEX derivatives), so spread trading needs both legs concurrent. **(a) Resolution rule:** a market's trio `(app_id, app_secret, app_code)` is resolved **independently** — complete per-market trio (`settrade_{equity,derivatives}_*`) wins; a **PARTIAL** per-market trio leaves the market UNCONFIGURED with a boot WARNING naming the missing field NAMES and **NO silent fallback** to the shared trio (a forgotten secret must never route a leg through the wrong app); else the shared `settrade_app_*` trio (the sandbox single-app path); mixed mode allowed. **(b) Client value-dedupe:** markets resolving to equal trios share ONE client (frozen `SettradeAppCredentials` keys a `by_creds` map — `SecretStr` is hashable + value-equal), so the sandbox keeps one login/session under both market keys; the adapter id-dedupes for heartbeat + `aclose`. **(c) All-sessions heartbeat on the single frozen breaker:** probe every distinct session, healthy ⇔ **all** configured apps alive; one dead app trips the ONE breaker + mass-cancels **both** books. **(d) `fetch_venue_orders` raises `SettradeMarketNotConfigured` for an unconfigured market — never `[]`.** **(e) Per-market reconciler budget skip** (`exhausted: set[Market]`); `get_budget_exhausted(market)` is per-market. **(f) Additive `/health` `brokers.settrade.sessions`** = `{"SET": bool\|None, "TFEX": bool\|None}`. Capability **cells**, the wire, `core/`/`db/`/`contracts/`, the PATCH route, `NormalizedOrder`, and `POST /orders` are all unchanged; a spread is two independent submits (no batch endpoint), in-engine (no new `third_party` service). | The `BrokerAdapter` base owns **exactly one** breaker per adapter (frozen Phase 0) — per-market breakers would thaw that invariant, and they are also the wrong semantics: a spread holds one leg per app, so a dead app leaves the other leg un-hedgeable; routing the survivor increases one-sided exposure, so trip + flatten both books is correct (`sessions` shows which app died). Partial-fails-loud guards the wrong-app-routing foot-gun. Raising-not-`[]` prevents an empty list from forging "venue says zero orders" and driving cancel_confirm/ack_lost against possibly-live rows. The per-market skip set fixes the old whole-pass `break`, which inverted starvation (one starved bucket stalled the healthy client's groups). 713 tests, 96.22% cov; real-venue read-only verified against prod broker 023 through the refactored adapter (equity `902001825` via `ALGO_EQ`, TFEX `507619-0` via `ALGO`; PIN never sent). The InnovestX trading PIN is still absent from `.env` — the explicit `micro_live`-flip prerequisite. |

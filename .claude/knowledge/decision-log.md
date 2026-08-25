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
| E21 | **[SUPERSEDED 2026-07-18 — broker-023/`settrade_v2` execution routing removed; see the "broker-023 / settrade_v2 removal" entry at the end of this log.]** **Raw `httpx.AsyncClient`, NOT the `settrade-v2` SDK.** The wire is re-implemented directly; shapes pinned from the SDK v2.2.1 source cross-checked against the scraped official venue docs (see E26). One new dep: `cryptography>=42` for ECDSA P-256 login signing. | The SDK is sync `requests` (forbidden in `src/` — blocks the loop) with import-time side effects unacceptable in a service: it writes `~/settradesdkv2_config.txt`, makes an NTP call, and fires a version-check HTTP request on import. Thread-pooling or vendoring it keeps those. |
| E22 | **Native amend orchestration.** Kill-switch gates amends **up front** (an amend can increase exposure — a documented asymmetry vs the un-gated cancel path); PTRM re-checks the hypothetical amended order with **NO exemption**; the commit is one `replace_order` UPDATE setting status+price+quantity atomically (`PENDING_REPLACE → NEW`) so the audit row snapshots the amended values; a partially-filled order does a two-step restore (`cancel_two_step` precedent → `NEW`, then `PARTIALLY_FILLED`). A venue amend-reject is a **NON-terminal restore** + typed `AmendRejected` (409, wire code `amend_rejected`) — the order is still live, so `reject_reason` is deliberately untouched (the two audit rows are the durable evidence). | The frozen 13-edge machine reserves `PENDING_REPLACE → NEW` for native amends with the price/qty columns unconstrained by the guard; a two-statement update would snapshot a stale price; writing `reject_reason` would imply the order is dead. No ADR/edge change, no infra-db migration — verified against the live trigger. |
| E23 | **Heartbeat = OAuth token-liveness probe** (vs E19's real venue health route). Healthy ⇔ `ensure_token()` succeeds AND `last_wire_ok is not False`; N consecutive failures (default 3) trip → `broker_circuit_open` + mass-cancel; healthy probe resets. The residual blind spot — a valid token whose order endpoints are degraded — is documented and fixed in Phase 5 (MQTT gives a real session signal). | Settrade exposes **no** health/session endpoint (Liberator has one — E19). Token acquirability IS the OAuth session; waiting for a 401 on a live order is unacceptable (ADR §G). |
| E24 | **Reconciler mirrors Liberator (mirror-not-abstract); extraction to a shared base deferred to Phase 6.** Fills are cumulative-watermark deltas (E18): `broker_fill_id = f"{order_no}:{matched}"`, ON-CONFLICT-dedupe; §B constants verbatim (5 s stuck, 60 s `ack_lost_unmatched`, ±5 s fuzzy, unique-candidate-only); grouped per `(account, market)`. **New action `replace_resolve`** repairs a stranded `PENDING_REPLACE` (crash/lost response mid-amend): venue resting → `replace_order(venue price/qty)`; venue terminal → resolve then legal close-out; venue missing → restore local values. `fetch_orders_for_reconcile(include_pending_replace=True)`. | A verbatim structural mirror is the lower-risk move for a second adapter; premature abstraction over two examples buys nothing. Native amend introduces a new stranded local state the §B watermark machinery doesn't cover. |
| E25 | **Rate limits = observe-don't-throttle (v1).** The client parses `X-RateLimit-*` into a `rate_snapshot()`; the adapter exposes `get_budget_exhausted()` (the reconciler does NOT reach into the client's private rate state); the reconciler **budget-skips** remaining `(account, market)` groups when the GET bucket is exhausted; a zero-remaining read logs a WARNING. GET and POST+PATCH live in **separate buckets** so reconcile reads never starve order writes. Adaptive submit-path throttling is Phase 6. | Observe-first is the safe default for a real-money submit path; throttling the writes risks masking a real backlog. The `get_budget_exhausted()` seam keeps the reconciler off the client's internals. |
| E26 | **[SUPERSEDED 2026-07-18 — broker-023/`settrade_v2` execution routing removed; the `developer.settrade.com` doc-pinning recipe + `broker-research-settrade.md` are removed with it. See the "broker-023 / settrade_v2 removal" entry at the end of this log.]** **SET equity scope = operator amendment; conservative cells replaced by doc-pinned cells.** The Phase-0/old-ROADMAP "no SET on Settrade (derivatives first)" non-goal was **struck by operator decision** — Phase 4 ships 100% of the settrade-v2 INVESTOR order surface (SET + TFEX). The former `(confirm P4)` enum cells were pinned from the **official venue docs**, discovered to be served as raw markdown by the `developer.settrade.com/template/open-api/...` backend (menu `config.json` + `{n}_{name}.md` pages) — superseding the Phase-3-era "the SPA cannot be scraped" note. | The SPA renders its content from a scrapable markdown backend; that discovery turned the enum-pinning fallback (guess conservatively, validate at micro_live) into verified cells before any code shipped. Recipe recorded in `broker-research-settrade.md` as the doc-pinning vehicle for future phases. |
| E27 | **`wire_price` float-exactness (reject, never re-quantize)** + `price=0` rule + stop-condition side derivation. `Decimal → float` only when the float's `repr` round-trips exactly back to the `Decimal`, else a typed reject. `ATO`/`ATC`/`MP-MTL`/`MP-MKT` (and the `STOP` market leg) send `price: 0`. Stop side: `BUY → 'LAST_PAID_OR_HIGHER'`, `SELL → 'LAST_PAID_OR_LOWER'`, `stopSymbol = order.symbol`. | Money is `Decimal` at the boundary (umbrella rule); the venue wire takes JSON numbers, so the float crossing must be provably lossless or rejected. The contract has `stop_price` but no condition field — the side derives the only safe v1 condition; `SESSION`/explicit conditions stay out of scope (E26). |

## Phase 4.1 realisation decisions (2026-06-11)

| # | Decision | Rationale |
|---|---|---|
| E28 | **[SUPERSEDED 2026-07-18 — broker-023/`settrade_v2` execution routing removed; see the "broker-023 / settrade_v2 removal" entry at the end of this log.]** **Settrade per-market broker apps — one `SettradeClient` per market behind the unchanged adapter.** The real broker **InnovestX (023)** splits the books across two OAuth apps (`ALGO_EQ` = SET equity, `ALGO` = TFEX derivatives), so spread trading needs both legs concurrent. **(a) Resolution rule:** a market's trio `(app_id, app_secret, app_code)` is resolved **independently** — complete per-market trio (`settrade_{equity,derivatives}_*`) wins; a **PARTIAL** per-market trio leaves the market UNCONFIGURED with a boot WARNING naming the missing field NAMES and **NO silent fallback** to the shared trio (a forgotten secret must never route a leg through the wrong app); else the shared `settrade_app_*` trio (the sandbox single-app path); mixed mode allowed. **(b) Client value-dedupe:** markets resolving to equal trios share ONE client (frozen `SettradeAppCredentials` keys a `by_creds` map — `SecretStr` is hashable + value-equal), so the sandbox keeps one login/session under both market keys; the adapter id-dedupes for heartbeat + `aclose`. **(c) All-sessions heartbeat on the single frozen breaker:** probe every distinct session, healthy ⇔ **all** configured apps alive; one dead app trips the ONE breaker + mass-cancels **both** books. **(d) `fetch_venue_orders` raises `SettradeMarketNotConfigured` for an unconfigured market — never `[]`.** **(e) Per-market reconciler budget skip** (`exhausted: set[Market]`); `get_budget_exhausted(market)` is per-market. **(f) Additive `/health` `brokers.settrade.sessions`** = `{"SET": bool\|None, "TFEX": bool\|None}`. Capability **cells**, the wire, `core/`/`db/`/`contracts/`, the PATCH route, `NormalizedOrder`, and `POST /orders` are all unchanged; a spread is two independent submits (no batch endpoint), in-engine (no new `third_party` service). | The `BrokerAdapter` base owns **exactly one** breaker per adapter (frozen Phase 0) — per-market breakers would thaw that invariant, and they are also the wrong semantics: a spread holds one leg per app, so a dead app leaves the other leg un-hedgeable; routing the survivor increases one-sided exposure, so trip + flatten both books is correct (`sessions` shows which app died). Partial-fails-loud guards the wrong-app-routing foot-gun. Raising-not-`[]` prevents an empty list from forging "venue says zero orders" and driving cancel_confirm/ack_lost against possibly-live rows. The per-market skip set fixes the old whole-pass `break`, which inverted starvation (one starved bucket stalled the healthy client's groups). 713 tests, 96.22% cov; real-venue read-only verified against prod broker 023 through the refactored adapter (equity `902001825` via `ALGO_EQ`, TFEX `507619-0` via `ALGO`; PIN never sent). The InnovestX trading PIN is still absent from `.env` — the explicit `micro_live`-flip prerequisite. |

## Phase 5 realisation decisions (D14–D24, 2026-06-12)

> **Phase 5 continues the cross-cutting Decision Log D-series (D1–D13), not this repo's
> per-service E-series.** The phase plan
> ([`docs/plans/phase5-strategy-execution-path-order-streaming.md`](../../docs/plans/phase5-strategy-execution-path-order-streaming.md))
> numbers these D14–D24 because order-update streaming is the realisation of umbrella **D12**
> ("normalized order-update stream out") — an umbrella-level contract decision, not a
> service-local mechanics choice like E7–E28. The open questions continue the ADR's §A–§G as
> **§H–§K** (see the ROADMAP). **Engine side only** — the strategy-repo flags + sim trade loop
> split to **Phase 5.1**. 853 tests, 95.72% cov; commits `3530fc4` / `f1e8991` / `92c30b5`
> (+ plan-doc `dcc84ee`); `strategy_id` migration is `quant-infra-db` PR #15 (open).

| # | Decision | Rationale |
|---|---|---|
| D14 | **SSE, not WebSocket; hand-rolled, no new dependency.** Order updates over `StreamingResponse(media_type="text/event-stream")`; reconnect via the standard `Last-Event-ID`. `sse-starlette` declined. | Updates are strictly server→client — WS buys bidirectionality we don't need at the cost of upgrade handling in the gateway proxy. The keep-alive + disconnect handling is ~30 lines we type strictly ourselves; one fewer dependency on the real-money path. |
| D15 | **In-process `EventHub` hooked inside the FIVE repository write functions** — `insert_order` (the `PENDING_NEW` birth) + `ack_order` + `replace_order` + `update_status` + `apply_fill`. Publish is **post-success, synchronous, non-blocking** (`put_nowait`; a full subscriber queue drops oldest + enqueues a `gap` advisory); a process-monotonic `seq` doubles as the SSE event id; a ring buffer (default 1024) replays on reconnect, else `resync_required`. Publish sits behind **one sanctioned broad `except`** (logs loudly, never re-raises). | Exploration confirmed all 13 frozen edges (incl. the kill-switch mass-cancel) funnel through exactly these five functions — hooking them guarantees **no transition is missed**. **The stream is advisory; the durable store is truth** — the streaming analogue of §A's at-least-once + reconcile doctrine; stream plumbing must never block, slow, or fail a committed write. |
| D16 | **Strategy identity = `X-Strategy-Id` header, persisted; shared API key unchanged.** The header (slug-validated) stamps the new **nullable `execution.orders.strategy_id`** column (own infra-db PR #15); events echo it; `GET /orders/stream?strategy_id=` filters on it, **DB-seeding** a strategy's historical cids at subscribe time so reconciler-discovered events for pre-restart orders still match; `cancel_replace` replacements inherit it. | The frozen `NormalizedOrder` is untouched — identity is transport metadata, not order data. Per-strategy keys/JWT are deferred (§J); the header is trusted the same way the order payload is, and the durable column makes that upgrade additive. |
| D17 | **The order book service is an in-engine, read-only, lossy-tolerant consumer cache.** It **consumes** broker feeds read-only, holds an **in-memory** cache only (no durable storage, no canonical ownership — `quant-marketdata-engine` stays the OHLCV owner); a dropped tick is a resubscribe, never a loss. | ADR §G ("streaming creep") pinned D1: market-data streams are external read-only dependencies. This stays inside that carve-out — its purpose is execution-plane (paper-fill realism now, PTRM price-band checks later). The umbrella ADR gains an addendum note, not an edge change. |
| D18 | **[SUPERSEDED 2026-07-18 — the Settrade order-book provider is removed; the order book is now Liberator-only (single provider). See the "broker-023 / settrade_v2 removal" entry at the end of this log.]** **Settrade order-book source = `settrade-v2` SDK realtime, CONTAINED.** The bid/offer feed rides `subscribe_bid_offer`; the SDK is imported **lazily inside `providers/settrade.py` only** (nothing else imports it), and all blocking SDK work runs on `asyncio.to_thread` with the sync callback bridged via `loop.call_soon_threadsafe`. | The **E21 SDK ban stands unchanged for the order-routing path** (sync `requests` blocks the loop; import-time side effects: `~/settradesdkv2_config.txt`, NTP call, version-check HTTP). The realtime feed is market-data only — its network loop runs on the SDK's own thread; the event loop is never blocked (tested). The SDK may **not** leak into the `adapters/` layer (this is why D23 is deferred). |
| D19 | **Liberator order-book source = ws-ticket + raw `websockets` Engine.IO v4 client — NO `curl_cffi`.** Ticket from the bundled `liberator-trading-api` (`POST /ws-ticket`); a minimal Engine.IO v4/Socket.IO client (read the open packet, pong server pings, connect the default namespaces `MarketStatusV2/TFEXDashboardV2/MarketIndexV2/StockV2/TickerV2` **before** `BidOfferV2`, resolve symbol→`orderBookId`, batch-join rooms); **mid-session live join/leave** on the open socket; jittered exponential backoff with a fresh ticket + re-join per attempt. | `curl_cffi` is a **hard constraint** (frequent disconnects in the legacy implementation). The default-namespace join order is a kept legacy finding. Mid-session join was a review finding — a session-start-only join silently starved mid-session subscribers until the next reconnect. |
| D20 | **[SUPERSEDED 2026-07-18 — with Settrade removed the order book has a single provider (Liberator), so failover cannot trigger; the `ProviderRouter` failover machinery is retained generic but dormant. See the "broker-023 / settrade_v2 removal" entry at the end of this log.]** **Provider failover: N consecutive errors within a window → silent switch + structured log.** `ORDER_BOOK_PRIMARY_PROVIDER` picks the default primary; a per-symbol `ORDER_BOOK_SYMBOL_OVERRIDES` map pins symbols. ≥ `FAILOVER_ERROR_THRESHOLD` consecutive errors from the **active** provider within `FAILOVER_WINDOW_SECONDS` → migrate non-overridden subscriptions to the secondary + log `order_book.provider_switch`. **No auto-failback** (v1). **Amended 2026-06-12 (operator): the default primary is `liberator`** — verified streaming live (AOT + S50M26), while InnovestX/Settrade realtime is venue-gated (`DISPATCH-UM-04` "User is inactive") until enabled at the portal. | The normalized `OrderBook` is identical from either source, so consumers never notice the switch. Flapping protection beats auto-failback for v1; a restart restores the primary. |
| D21 | **Sim fill-price chain: book cache → market-data engine → existing reference price.** `SimAdapter` gains an optional injected price source: warm cache fills BUY at best ask / SELL at best bid, **bounded by the order's own limit** (a fill never crosses its limit); cold → the market-data engine's last 1d close (limit-bounded, plain httpx GET); absent → the existing `_reference_price`. Every hop logs. **Price-only.** | The fill-PLAN semantics (FOK/IOC, `sim_fills`/`sim_reject`) are untouched, and **with no source injected the adapter is the bit-for-bit Phase-2 deterministic pure function** — existing tests and the frozen acceptance behaviour are preserved. The dependency arrow points one way: `SimAdapter` depends on a small `FillPriceSource` Protocol, never on `order_book`. |
| D22 | **Order book + event models are frozen Pydantic, not dataclasses.** The prompt sketched `@dataclass`; these models cross the API/SSE boundary serialized, so frozen `BaseModel`, `Decimal` prices, `int` volumes, tz-aware UTC, `Decimal`-as-string on the wire. | Umbrella hard rule 3 (Pydantic at module/external I/O boundaries) wins over the prompt's sketch. |
| D23 | **Settrade order-push reconcile "kick" — DEFERRED to §I.** A native order-push message would, at most, schedule an immediate reconcile pass (the reconciler stays the sole writer — never mapping raw venue push onto frozen edges, which would bypass E20/E24 enforcement). **Not shipped:** wiring the SDK realtime *order* subscription would breach the D18 containment (the SDK may not leak into `adapters/`). | The stream is already fed by the reconciler + sim + router write paths, so the kick is pure latency optimisation; deferring it costs nothing and keeps the SDK boundary clean until the push protocol is observed at micro_live. |
| D24 | **Order-book endpoints are public-mode-readable; the whole service defaults OFF.** `GET /order-book/*` carries no order data, no credential, no raw broker payload → readable in public mode like the other reads. `ORDER_BOOK_ENABLED` defaults **false**: the service, its providers, and the SDK import never activate unless an operator opts in. | The engine's existing behaviour and the broker-free `docker compose up` default stay **bit-for-bit unchanged**. **No change to `live`/`micro_live` gating** — the feeds are read-only market data. |

## Phase 6 realisation decisions (E29–E35, 2026-06-13)

> **Phase 6 — safety, ops & reconciliation hardening — is service-local mechanics (the
> per-service E-series, like E7–E28), not a cross-cutting contract change.** No new feature,
> no new broker; everything is additive behind the unchanged frozen contracts and gating. The
> phase **revisits open question §H** (single-process fan-out, deferred in Phase 5) and pins it
> below. The plan
> ([`docs/plans/phase6-safety-ops-reconciliation-hardening.md`](../../docs/plans/phase6-safety-ops-reconciliation-hardening.md))
> Design Decisions §1–§8 are authoritative; this records the durable summary. **952 tests,
> 96.01% cov** (853 baseline → +99), mypy strict, ruff clean. **`live` stays gated; the frozen
> `NormalizedOrder` / 13-edge state machine / capability cells, kill-switch-first ordering, and
> PTRM semantics are all unchanged; no `quant-infra-db` schema change.**

**§H revisit conclusion (CONFIRMED single-process, NOT upgraded).** §H asked whether the
in-process `EventHub` fan-out should grow a multi-worker story (Redis pub/sub, mirroring the
kill-switch pattern). **Conclusion: confirmed single-process — Phase 6 adds NO multi-worker
fan-out.** The engine runs one uvicorn worker; the `EventHub`'s existing drop-oldest +
gap-marker overflow policy with its exception-proof `publish` already satisfies the requirement,
and it was **verified under a 1000 ev/s × 10-slow-subscriber stress test** (queue 256): fast
subscribers receive all events, slow subscribers receive `gap` markers on overflow, the publisher
never blocks, the order path never raises. D3 therefore shipped a **stress test only, no code
change**. Multi-worker / Redis pub-sub stays **deferred** until a concrete second-worker story
exists (the §H deferral stands; the durable store remains truth, the stream advisory).

| # | Decision | Rationale |
|---|---|---|
| E29 | **Unified duplicate-burst guard, default-ON (A3; Design Decision §1 — deviation from the prompt's `false`).** A `duplicate_burst` check already existed in `core/risk.py` (coarse `account\|symbol\|side\|qty` fingerprint, 2 s window, always-on, 429, env `risk_duplicate_burst_window_seconds`). Rather than ship a second cosmetic guard, the **single** existing guard was evolved to A3's contract: a richer fingerprint `account\|symbol\|side\|quantity\|order_type\|price` (`price=None` → the literal `"None"`, a distinct fingerprint), a configurable window via the new `duplicate_burst_window_seconds` (5 s default), Redis key `exe:burst:{sha256(...)[:16]}` (the account never appears in a key), a typed `DuplicateBurstDetected` → **409** (was 429; it is no longer a `RiskRejected` throttle cap — `_THROTTLE_CAPS` now holds only `rate_limit`), gated by `duplicate_burst_guard_enabled` **defaulting `true`**. The legacy `risk_duplicate_burst_window_seconds` is retained so old `.env` files load but is **no longer read** by the guard. | A *hardening* phase must never silently disable an active safety guard (the prompt's default-off would have done exactly that), so the flag defaults on. The richer fingerprint is strictly better — still catches exact economic duplicates, stops over-blocking a legitimate re-price, and 409-conflict is cleaner semantics than a 429 throttle. **Trade-off:** the default-on flag + the 429→409 change touched a few existing risk tests, and operators relying on the old 2 s/429 behaviour see new semantics (documented here + in `.env.example`; set the flag `false` to disable entirely). |
| E30 | **`TokenBucket` algorithm — pure asyncio, monotonic lazy-refill, await-on-deficit (D1/D2; Design Decision §7).** One shared `adapters/rate_limit.py` `TokenBucket`: capacity = `rate_per_second` (a 1-second burst), starts full; on each `acquire` it lazily refills from elapsed **monotonic** time under an `asyncio.Lock`; if a whole token is available it consumes and returns, else it computes the deficit, emits **exactly one** `<name>_rate_limit_wait` WARN with the wait duration, `await asyncio.sleep(deficit)`, then consumes. It **never busy-spins, never drops, never raises** a throttle to the caller; **`rate <= 0` ⇒ no-op** (an unlimited bucket so a `0` setting can never deadlock the submit path). The clock + sleep are injectable for deterministic tests. **Placement:** Settrade GET + WRITE buckets live **per `SettradeClient`** (per OAuth app/market — Design Decision §4, NOT one per `SettradeAdapter`), acquired in `request_json` after `ensure_token` (auth is unthrottled) keyed by HTTP method; the Liberator POST bucket lives on `LiberatorAdapter`, acquired in `place()` ONLY (a mapping-rejected order consumes no token; cancel/heartbeat/reconciler fetches stay unthrottled). | The prompt forbids a third-party rate-limit library; a lazy-refill bucket is the minimal correct primitive and is trivially testable with an injected clock + recording sleep. **Await-on-deficit IS the back-pressure** (intended); a rate-limited request still goes through (eventually-successful or broker-rejected) — the caller never sees a throttle. **Per-client (not per-adapter):** Settrade enforces its budget **per OAuth app**, and Phase 4.1 runs one client per market — a single per-adapter bucket would wrongly throttle SET and TFEX together, under-using each app's independent allowance. **Trade-off:** a small documented deviation from the prompt's literal "per-`SettradeAdapter` instance", chosen for venue-correctness; FIFO fairness via the lock is sufficient at this order volume. |
| E31 | **Audit response is SYNTHESIZED from the existing `order_events` columns — NO `quant-infra-db` schema change (E1/E2; Design Decision §3).** The store has `from_status`/`to_status`/`event` (JSONB)/`created_at` only — **no `event_type` and no `metadata` column.** The `api/audit.py` read API derives every audit-friendly field at read time: `seq` ← a 1-based per-order ordinal (`fetch_order_events` orders by the monotonic `event_id`, stable even when `created_at` ties inside one transaction); `broker_order_id` + `metadata` ← the opaque `event` JSONB (surfaced verbatim as `metadata`); `event_type` ← a **pure total `(from_status, to_status)` mapping** (`create`/`ack`/`replace`/`fill`/`cancel_request`/`cancel`/`replace_request`/`reject`/`expire`, with a safety default); `occurred_at` ← `created_at` as UTC ISO-8601. The export streams via an asyncpg **server-side cursor** (`prefetch=500`) so a large date range never buffers in memory. Both routes are **reads only** and owner-mode (403 `problem+json` in public mode); the `event` JSONB is decoded once so the NDJSON line carries a real object, not a quoted string. | The append-only audit store is the separate `quant-infra-db` repo's concern; Phase 6 ships from the engine repo only and adds a **read path**, not a migration. `event_type` is derived, not stored — a future schema that stores it natively would supersede the mapping (additive, no break). |
| E32 | **Price-band check is advisory, reuses a factored-out shared `MarketDataClient`, and slots AFTER the PTRM gate (A2; Design Decisions §3-of-the-plan / §8).** New `core/price_band.py` `PriceBandCheck`; the market-data last-close fetch was **factored out of `adapters/sim_pricing.py` into a shared `adapters/market_data.py` `MarketDataClient`** (a process singleton; the per-request `OrderRouter` borrows it, never owns it) so the price-band check and the SimAdapter fallback hop share one client. Wired into `router.submit` **after** the existing PTRM risk gate and **before** any adapter routing. With the flag off (or no market-data client / unpriced MARKET order) `check()` is a **no-op**; a market-data **fetch failure is advisory — WARN + pass** (never a hard gate); an outside-band LIMIT order → typed `PriceBandExceeded` (422). | Preserves the **kill-switch-first** invariant (hard rule 6 — the switch is still checked first) and keeps the optional network-touching band hop off the hot path for already-rejected (capped/malformed) orders. Sharing one `MarketDataClient` avoids two near-identical httpx clients + parsers. **Trade-off:** one awaited market-data GET on the submit path when enabled, bounded by the advisory WARN+pass-on-failure contract. |
| E33 | **Kill-switch admin-trip hardening — idempotent, structured-logged, operator-attributed (B1).** `POST /admin/kill-switch/engage` is now status-first idempotent: a second engage returns 200 `already_engaged=true` with `cancelled_count=0` and runs **no second mass-cancel**; the first engage trips the switch, sweeps open orders, and emits a structured JSON `kill_switch.engaged` log (`operator` + `cancelled_count` + `failed_count`, never a secret), returning `cancelled_count` alongside the existing `cancelled`/`failed` cid lists (additive schema). `POST /admin/kill-switch/disengage` is status-first too: a clear switch → typed **`KillSwitchNotEngagedError` (409 `kill_switch_not_engaged`)**, distinct from the env-pinned 409 (`kill_switch_env_pinned`, which still wins); on success it emits a structured `kill_switch.disengaged` log. Both endpoints take an **optional `X-Operator-Id`** header (`get_operator_id` dep — trimmed value or `"anonymous"`; it **never raises**, identity is advisory audit context, not an auth gate). Because `status()` reports not-engaged when Redis is absent, the disengage route's only `CacheError` path became unreachable and was removed — a clean 409 instead of a 503. | The trip is a real-money flatten-and-halt; making engage/disengage idempotent + structured-logged + operator-attributed is the auditability the runbook needs. The 409-not-engaged is the correct conflict for a redundant disengage (vs the env-pinned conflict). Operator identity stays advisory so a missing header can never block the safety action. |
| E34 | **B2 kill-switch fault test asserts GENUINE CANCELLED-transition audit rows + the structured log — no literal `kill_switch_cancel` event_type (Design Decision §6).** The DB trigger writes `order_events` rows keyed by transition (`PENDING_CANCEL → CANCELLED`); the app cannot inject a literal `event_type="kill_switch_cancel"` row without an out-of-scope infra-db trigger change. So the 5-order (NEW + PARTIALLY_FILLED) fault test asserts: all 5 transition to `CANCELLED` in `execution.orders`; each has its **genuine** CANCELLED-transition audit row (the real append-only mechanism); a fresh submit is rejected `kill_switch_engaged`; disengage → a fresh submit is accepted. The kill-switch context lives in the structured `kill_switch.engaged` log (with the mass-cancel count), not a forged event_type. | Faithful to the **actual** append-only audit machinery without a schema change — asserting the real CANCELLED rows is a stronger test than asserting a label the store doesn't write. **Trade-off:** the E31 derivation surfaces these as `cancel` (via `cancel_request` then `cancel`), not `kill_switch_cancel`; the engage-response `cancelled_count` + the structured log carry the kill-switch framing. |
| E35 | **C/D test-double fidelity fixes — no production change.** Two test-infrastructure fixes landed with the soak/stress suites: a `MemStore.apply_fill` fidelity fix so the in-memory test double matches the real repository's fill-insert/publish semantics (the soak tests drive the engine's persistence + reconciliation directly with the broker layer mocked), and **D3 confirmed §H needed NO `EventHub` code change** (the existing drop-oldest + gap-marker policy passed the stress test as-is). | The fault-injection + stress suites exercise real engine logic against fakes; the fake must not diverge from the production seam it stands in for, or the test proves nothing. Recording these keeps "what changed in `src/` vs `tests/`" honest: Phase 6 added new modules + risk/route/adapter wiring, but the §H verification and the burst-test double were test-only. |

## broker-023 / settrade_v2 removal — Streaming-Pro-only execution routing (DECISION, 2026-07-18)

> **This is the forward record.** The Phase-4/4.1/5 Settrade entries above (E21, E26, E28, D18,
> D20) are marked **SUPERSEDED (2026-07-18)** in place — left as period records, not rewritten.
> `feature-execution-engine` Option B: full removal of the Settrade Open-API execution path;
> Sim + Liberator + Streaming Pro retained.

1. **DECISION.** Real-money execution routing is now **exclusively the self-built Streaming Pro
   system** (the `broker-api/settrade-streaming-api/` retail bridge behind `StreamingProAdapter`):
   **FINANSIA account 024 on the HOME node, SBITO account 033 on the AWS node**. The `settrade-v2`
   library and the **InnovestX broker-023 (Settrade Open API, `SettradeAdapter`)** execution path
   are **removed entirely** — adapter, order-book provider, config fields, the `settrade-v2`
   dependency, tests, and the two Phase-4 plan docs. `SimAdapter` and `LiberatorAdapter` are
   unchanged.

2. **REASON.** Broker fees + `settrade-v2` library limitations (the SDK is sync `requests` that
   blocks the event loop, with unacceptable import-time side effects — the same limitations that
   already forced the E21 raw-httpx re-implementation; the operator's decision retires the whole
   Open-API path rather than keep carrying it).

3. **CORRECTED IDENTITY (authoritative).** **024 = FINANSIA** and **033 = SBITO**, both reached
   **via Streaming Pro** (the retail bridge). **023 = InnovestX** — the *removed* Settrade Open
   API. **Streaming Pro ≠ InnovestX**: the kept self-built bridge is NOT the removed Open-API
   broker, and the two must never be conflated.

4. **PIN NUANCE (recorded precisely — NOT "never used ever").** The `EXECUTION_ENGINE_SETTRADE_PIN`
   was **tested for real in an UNRELATED prior project** and was **most likely copied forward into
   this repo's `.env` from an old file**. It was **never exercised in this codebase, nor against
   broker 023 as configured here** — DB-verified: `execution.orders` holds **29 rows, all
   `broker='sim'`, zero `settrade`**. (The removed config only ever loaded settings in `sim`; no
   real Settrade order was ever routed from this service.)

5. **TERMINOLOGY CONVENTION (authoritative, project-wide).** **"Streaming Pro" / `streaming_pro`**
   = the **self-built bridge** (`broker-api/settrade-streaming-api/`, `StreamingProAdapter`) —
   **KEEP**. **"Settrade" / `settrade` / `settrade_v2`** = the **official Settrade Open API** —
   **REMOVED**. Never use bare "Settrade" to mean Streaming Pro. (The kept bridge's repo/container
   name `settrade-streaming-api` is Settrade's product name for the *retail app* it wraps — that
   is the KEEP side, distinct from the removed Open API.)

6. **NO forward-looking Open-API guidance.** Any future Settrade / Open-API connectivity is a
   fresh from-scratch decision; this log does **not** pre-suggest `settrade_v2` or the Open API as
   a path, and the `developer.settrade.com` doc-pinning recipe (E26) is retired with the code.
   **Capability note:** *native* amend was Settrade-only among real brokers, so the engine loses
   real-broker native amend — expected. Liberator + Streaming Pro use `cancel_replace`; only the
   `sim` simulator retains native amend. The `PATCH /orders` route + the cancel_replace path are
   intact.

## Post-placement handle recovery + the `resolution` contract (DECISION, 2026-08-25)

Realises [[TK-0423]]; fixes [[TK-0424]]. Shipped `ea11127` (PR #42), deployed to AWS `micro_live`
2026-08-25 12:41 BKK and verified live the same day.

1. **THE FACT THAT FORCED IT.** The Liberator place-ack carries **no `orderNo` at all** — measured on
   both order classes (9 FOKs terminal on arrival; 1 DAY LIMIT that rested), so it is unconditional,
   not a terminal-on-arrival quirk. `.claude/knowledge/broker-research-liberator.md` had asserted the
   opposite since it was written; nothing had checked it against a real placement.

2. **THE DEFECT.** `core/router.py` raised `AdapterError` when the ack carried no handle, under a
   `# pragma: no cover - adapter contract`. That surfaced as **HTTP 500 for an order the venue had
   ACCEPTED** — a caller cannot distinguish *"it failed"* from *"it is live and I lost the handle"*,
   which for a legging strategy means neither sending leg 2 nor unwinding leg 1. ⚠️ **The pragma is
   how it survived:** it asserted the branch was unreachable, which was true when written and was
   falsified by PR #41's adapter-side change — while the pragma kept the ≥90 % coverage gate silent.
   **A change that relaxes a contract must re-read the `pragma: no cover` lines that cite it.**

3. **DECISION — the raise becomes the read, not a deletion.** Deleting the raise would persist
   `PENDING_NEW` with a null handle and leave the caller equally uninformed, just with a 200. The
   router reaches that line **exactly** when it lacks the handle, which is **exactly** what a
   venue-truth read recovers, so the burst is placed there. One change, both defects.

4. **THE TWO FLOORS, both measured — neither is a preference.**
   - cadence **250 ms** ≈ the bridge read itself (200–244 ms). Faster cannot be fresher; it queues
     reads on a venue session **shared with the capture plane**, whose data is not backfillable.
   - budget **1500 ms**, anchored on the **persisted submit timestamp, not the ack** — the venue
     reaches terminal at 567–752 ms while our placement round-trip is 959–1,175 ms, so an
     ack-anchored clock starts ~400 ms late and no cadence recovers that.

5. **STOPS ON HANDLE RECOVERED, NEVER ON TERMINAL.** A resting order has no terminal state; waiting
   for one would hang the POST until the close. Resting-vs-terminal is reported, never waited on.

6. **SHARED CODE, NOT A PARALLEL COPY.** The reconciler's per-row body was **extracted** so the burst
   and the steady loop use one matcher and one executor. Copy-paste between exactly these two
   produced the TK-0036/37/90 back-ports. `resolve_order_now()` bypasses the 5 s lost-ack gate, which
   is sound **only there**: the gate exists so the steady loop cannot fuzzy-match an order whose ack
   is still in flight, and this path runs only *after* an ack that already returned empty. It
   **raises** on an unreadable venue rather than returning `False`.

7. **CALLER CONTRACT — `resolution: confirmed | pending | unknown`** on the `POST /orders` body only.
   🔴 `pending` (venue **was** read; order working) and `unknown` (venue **not** read; order may be
   LIVE with its handle unrecovered) must never share a field, a code path, or a default — only
   `unknown` is dangerous, and a resubmit on it double-fills. `GET /orders/{cid}` deliberately omits
   the field: a later read is not evidence about what was known at submit time.

8. **THE GUARANTEE IS STRUCTURAL, NOT STATISTICAL.** `POST /orders` cannot return before the venue has
   been read at least once, so submit-to-known is bounded by the call's own latency rather than by the
   12 s reconcile interval. Measured live: **1,205 ms**, against 8,865 ms before — inside the
   operator's stated 2 s bar with 40 % margin.

9. **NOT BUILT, deliberately: per-account burst coalescing.** `orders/{account}` returns the whole
   list, so one burst could serve every in-flight order on that account. Deferred because the only
   consumer legs **sequentially** and SET/TFEX are **different accounts** — no concurrency to
   coalesce — and this is a real-money path where the smaller change is the better one. Recorded on
   [[TK-0423]]; revisit if concurrent same-account submits appear.

Wire facts (venue side): [`docs/reference/liberator-order-wire.md`](../../../docs/reference/liberator-order-wire.md) (umbrella).

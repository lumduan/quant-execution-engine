# Order-update stream — schema + contract

> **SHIPPED in Phase 5 (engine side, 2026-06-12)** — the realisation of umbrella **D12**
> ("normalized order-update stream out"). Every order state transition the durable store
> commits is mirrored, in order, to subscribed clients over **SSE** with no polling
> (decisions **D14/D15/D16/D22** —
> [`decision-log.md`](decision-log.md#phase-5-realisation-decisions-d14d24-2026-06-12)). The
> stream is **advisory; the durable store is truth** — the streaming analogue of ADR §A's
> at-least-once + reconcile doctrine. A subscriber that misses an event re-reads
> `GET /orders/{client_order_id}`. Source:
> `src/quant_execution_engine/events/` (`models.py`, `hub.py`) +
> `src/quant_execution_engine/api/streams.py`.

## Endpoint

```
GET /orders/stream
  ?strategy_id=<slug>          # optional filter (conjunctive)
  &client_order_id=<cid>       # optional filter (conjunctive)
  &last_event_id=<seq>         # reconnect cursor (query fallback)
Header: X-API-Key: <shared key>          # api-key-gated, but PUBLIC-MODE READABLE
Header: Last-Event-ID: <seq>             # reconnect cursor (header wins over query)
→ text/event-stream
   Cache-Control: no-cache
   X-Accel-Buffering: no                 # tells nginx/proxies NOT to buffer
```

Gateway-proxied under `/api/v2/engines/execution/orders/stream`; the gateway must use a
non-buffering streaming proxy (`httpx` `client.stream()` → `StreamingResponse`) — it forwards
a plain chunked HTTP response, no WebSocket upgrade (the reason D14 chose SSE).

This is a **READ**, not an owner-gated submit: events carry **no raw broker payload, no account
number, no PIN/token** — `broker_order_id` is a venue id and is safe. So it answers in public
mode like the other reads.

## Frame format

The SSE `event:` field is **exactly the engine-state string reached** — a strict subset of the
frozen 9-state machine — plus the two advisory frames the route emits directly. No invented
states, no new edges.

```text
id: <seq>                                # process-monotonic int = ring-buffer cursor
event: <engine_state>                    # PENDING_NEW | NEW | PARTIALLY_FILLED | FILLED
                                         # | PENDING_CANCEL | PENDING_REPLACE | CANCELLED
                                         # | REJECTED | EXPIRED
data: <OrderUpdateEvent JSON>

# advisory frames (no id:)
event: gap
data: {"dropped": <n>}                   # this subscriber lagged; n events were dropped

event: resync_required
data: {"after_seq": <cursor>}            # reconnect cursor fell off the ring — re-read the store

# keep-alive (a comment line, every STREAM_KEEPALIVE_SECONDS, default 15)
: keep-alive
```

### `OrderUpdateEvent` JSON shape

Frozen Pydantic (`events/models.py`), `model_dump(mode="json")` on the wire — `Decimal`-as-string,
`datetime` ISO-UTC, matching the order contract (D22).

```jsonc
{
  "seq":             123,                 // == the SSE id:
  "client_order_id": "8f1c…",
  "strategy_id":     "csm-set" ,          // or null
  "engine_state":    "PARTIALLY_FILLED",  // internal 9-state truth
  "status":          "PARTIALLY_FILLED",  // frozen public status, via to_public_status (E8)
  "broker_order_id": "OD-00042",          // or null (pre-ack); a venue id, never a secret
  "price":           "101.500000",        // populated on replace/amend events, else null
  "quantity":        100,                 // populated on replace/amend events, else null
  "fill": {                               // present ONLY on fill events, else null
    "broker_fill_id": "OD-00042:100",
    "price":          "101.500000",       // Decimal-as-string
    "quantity":       100,
    "exec_ts":        "2026-06-12T03:20:42Z"
  },
  "ts":              "2026-06-12T03:20:42Z"
}
```

`status` is derived through **the one existing engine-state→public-status mapping**
(`to_public_status`, E8) with a zero fill watermark: the per-event hook does not re-aggregate
fills, so the transient pending states (`PENDING_NEW`/`PENDING_CANCEL`/`PENDING_REPLACE`) surface
as `NEW` while every terminal and fill state maps unconditionally. Read the truthful
`engine_state` for the exact internal state.

### Hook → event mapping

`event:` is whatever engine state the transition reached:

| Repository writer | event(s) emitted |
|---|---|
| `insert_order` | `PENDING_NEW` (the "accepted" event — the order's birth) |
| `ack_order` | `NEW` |
| `apply_fill` | `PARTIALLY_FILLED` / `FILLED` (+ the `fill` payload) — only for **newly-inserted** fills (redeliveries emit nothing) |
| `replace_order` | `NEW` (the amended `price`/`quantity` populated) |
| `update_status` | the target state — cancel walk, expiry, post-ack rejects, amend restore, **kill-switch mass-cancel sweep** |

## Internals — the EventHub (D15)

Single in-process `EventHub` (`events/hub.py`), created in the app lifespan **before** the DB
pool so no durable transition can beat it into existence:

- **Publish is hooked inside the five repository write functions**, post-success — the proven
  choke points all **13 frozen state-machine edges** funnel through (incl. the kill-switch
  mass-cancel). It is **synchronous, non-blocking** (`put_nowait`) and sits behind **one
  sanctioned broad `except`** that logs loudly and never re-raises: stream plumbing can never
  block, slow, or fail a committed durable write.
- **`seq`** from `itertools.count(1)` is the SSE `id:` and the ring cursor.
- **Ring buffer** (`collections.deque(maxlen=STREAM_RING_BUFFER_SIZE)`, default 1024) holds the
  recent window for `Last-Event-ID` replay.

### Reconnect semantics

The route subscribes (live tap) **first**, then replays the ring after the cursor — so no event
between subscribe and replay is lost; the subscribe/replay overlap is deduped by skipping events
at/below the max replayed `seq`.

- `Last-Event-ID` header (or `last_event_id` query fallback; header wins). An unparseable value
  is a **422** (not a silent reset — a client asking for replay must see a typo).
- `after_seq <= 0` ⇒ live-only (no replay).
- Cursor still inside the ring ⇒ the gap replays in order; nothing missed.
- Cursor fell off the ring ⇒ exactly one **`resync_required`** advisory; the client re-reads
  `GET /orders/{client_order_id}` for ground truth.

### Back-pressure (lossy-tolerant)

Per-subscriber bounded queue (`STREAM_SUBSCRIBER_QUEUE_SIZE`, default 256). On overflow the
queue **drops its oldest item** and records a `GapMarker`, surfaced once as an `event: gap`
frame carrying the accumulated `dropped` count (a burst of drops after the marker was enqueued
collapses to a single final count). A slow subscriber never blocks the publisher or any other
subscriber.

## Strategy identity + filtering (D16)

- **Stamp:** `POST /orders` accepts an optional **`X-Strategy-Id`** header (slug-validated, 422
  on violation) → the new nullable **`execution.orders.strategy_id`** column (`quant-infra-db`
  PR #15). `cancel_replace` replacements inherit it. Absent header = bit-for-bit prior behaviour.
  The frozen `NormalizedOrder` contract is **untouched** — identity is transport metadata.
- **Echo:** events carry `strategy_id`; the hub also keeps a bounded cid→strategy LRU.
- **Filter:** `?strategy_id=` (and/or `?client_order_id=`, conjunctive). At subscribe time the
  route **DB-seeds** the strategy's historical `client_order_id`s from the durable store via
  `fetch_client_order_ids_for_strategy`, so a later anonymous event for the same order (e.g. an
  `ack` published without strategy attribution after a restart) still matches. A strategy match
  additively seeds the event's cid into that set.
- Per-strategy keys/JWT scoping is deferred (**§J**) — the shared `X-API-Key` + header model
  ships now; the durable column makes that upgrade additive.

## Single-process note (§H)

The hub is in-process and the engine runs **one uvicorn worker** (compose). Multi-worker /
multi-instance fan-out (Redis pub/sub, mirroring the kill-switch pattern) is **deferred until a
second worker exists** — revisit in Phase 6.

## Smoke (curl -N)

Direct against the engine (host `:8400`, owner/public mode both answer):

```bash
# tail live order updates for one strategy (no replay)
curl -N -H "X-API-Key: $EXECUTION_ENGINE_API_KEY" \
  "http://localhost:8400/orders/stream?strategy_id=csm-set"

# reconnect from a known cursor (replays the ring after seq 123, then live)
curl -N -H "X-API-Key: $EXECUTION_ENGINE_API_KEY" -H "Last-Event-ID: 123" \
  "http://localhost:8400/orders/stream"
```

Through the gateway (verifies the proxy does not buffer — frames must arrive incrementally):

```bash
curl -N -H "X-API-Key: $INTERNAL_API_KEY" \
  "http://localhost:8000/api/v2/engines/execution/orders/stream?strategy_id=csm-set"
```

## Strategy consumer contract (Phase 5.1)

How the strategy repos (`csm-set`, `tfex-s50-multi-tf-swing`) consume this stream — the
client-side invariants their `ExecutionEngineAdapter` / `run_sim_loop` implement (Phase 5.1;
plan: [`../../docs/plans/phase5.1-strategy-execution-flags-sim-trade-loop.md`](../../docs/plans/phase5.1-strategy-execution-flags-sim-trade-loop.md)):

- **Gateway-only, attributed.** All calls ride `/api/v2/engines/execution/*` with `X-API-Key`
  + `X-Strategy-Id` on every request (the gateway forwards both since its PR #24); the stream
  filter is the `?strategy_id=` query param.
- **Subscribe before submit.** One SSE connection per strategy run, opened **before** the
  first `POST /orders`. The `SimAdapter` fills synchronously, so the POST ack can already be
  `FILLED` — subscribing first guarantees those transition events are still delivered, not
  raced.
- **Single-source fill accounting.** Position deltas are applied **only** from stream `fill`
  payloads; the POST ack never moves a position. Reconcile fallback: on per-order timeout or
  stream reset, `GET /orders/{cid}` and apply the **residual** (`filled_qty − applied_qty`)
  at `avg_fill_price`. This makes the ack-already-FILLED + replay overlap a non-event.
- **Client seq watermark.** The adapter tracks the max delivered `seq` and drops anything at
  or below it — the guard for duplicates across *client* reconnects (the server only dedupes
  its own subscribe/replay overlap). Reconnects send `Last-Event-ID: <watermark>`.
- **Advisories.** `resync_required` → the adapter raises a typed `StreamResetError(after_seq)`
  and the loop degrades to GET-polling for in-flight orders. `gap` → WARNING + continue (the
  timeout + GET-residual path recovers a lost terminal event).
- **Same-cid transport retry.** A bare transport 5xx/timeout retries the **same**
  `client_order_id` (ADR §A at-least-once + dedupe; a 200 prior-ack parses like the 201). A
  typed `{"error": …}` envelope — including 503 `kill_switch_engaged` — is terminal, never
  retried. Only a new *logical* order gets a fresh UUIDv4.
- **Local wire mirrors.** Strategies define their own Pydantic mirrors of
  `OrderUpdateEvent`/`NormalizedOrder*` (Decimal-as-string, `Literal` enums) — no cross-repo
  imports; this file + the phase plan are the schema source of truth for those mirrors.

## Cross-references

- Decisions D14–D24 → [`decision-log.md`](decision-log.md#phase-5-realisation-decisions-d14d24-2026-06-12)
- Order book service (the other Phase-5 stream) → [`order-book-service.md`](order-book-service.md)
- State machine + public-status mapping → [`order-state-machine.md`](order-state-machine.md),
  [`normalized-order-contract.md`](normalized-order-contract.md)
- Phase plan → [`../../docs/plans/phase5-strategy-execution-path-order-streaming.md`](../../docs/plans/phase5-strategy-execution-path-order-streaming.md)

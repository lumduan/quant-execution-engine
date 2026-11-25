# API — `GET /accounts/{account}` and `GET /accounts/{account}/open-orders`

Owner-mode. Gateway-proxied at `/api/v2/engines/execution/accounts/*`.

## Why these exist

So a strategy **never has to know which broker it is talking to**. Liberator and Streaming Pro have
nothing in common at the wire — different URLs, different payloads, different money-field names. A
strategy reading brokers directly learns both dialects, and grows a third when a third broker lands.

⇒ that leak **compounds per broker** rather than staying constant, which is why these are worth
routes even though a direct bridge read is safe. Operator ruling, 2026-08-27 (GH #234). The design
test: *a route belongs in the engine if a strategy would otherwise have to know which broker it is
talking to.*

## `GET /accounts/{account}?broker=…`

`broker` is a **required** query parameter — an account number does not name a broker, and guessing
one would be the same class of invention that produced [[TK-0396]].

```bash
curl "http://localhost:8400/accounts/70000002?broker=liberator" -H "X-API-Key: <key>"
```

```json
{
  "account": "70000002",
  "account_type": "cash",
  "buying_power": "50000.11",
  "cash_balance": "50000.11",
  "equity": null,
  "initial_margin": null
}
```

### 🔴 `null` means "this broker does not report it" — NEVER zero

**Do not re-collapse `null` into `0` on your side.** That collapse *is* [[TK-0396]]: a fabricated `0`
was returned for accounts holding real five-figure balances, and a confident zero is the shape that
passes a smoke test. The engine now refuses to invent one; a caller that maps `null → 0` reintroduces
the bug downstream of the fix.

Coverage is **deliberately asymmetric, because the venues are**. The margin block
(`equity`, `excess_equity`, `initial_margin`, `maintenance_margin`) is **DERIVATIVE-only** and is
*forbidden* on a cash account by a model validator — not merely absent. Money is Decimal-as-string.

## `GET /accounts/{account}/open-orders?broker=…`

**Venue truth, RESTING only.** Named `open-orders` rather than `orders` on purpose.

⚠️ It answers *"what is live at the venue right now"* — a **different question** from *"what happened
to my order"*. It is the venue's view rather than ours, it carries **no `client_order_id`** (the venue
echoes nothing the client sent, which is why the reconciler must fuzzy-match), and for Liberator the
venue list is **today-only**.

⇒ for history, joinable by your own `client_order_id` and spanning every stage, read the durable store
via [`GET /orders/{client_order_id}`](orders-get.md).

## Errors

| code | HTTP | meaning |
|---|---|---|
| `real_routing_not_authorized` | 409 | this node is not declared for that account (EH6 — applies to reads too) |
| `liberator_account_not_found` | 404 | the venue refused the account. **Never rendered as a zero balance** |
| `liberator_positions_uncaptured` | 501 | a position row whose schema has never been observed — refused rather than guessed |
| `read_key_forbidden` | 403 | the read-only key reached a route outside the account reads (never these routes) |
| `public_mode` | 403 | owner mode only; these expose real account financials |

## Order history — two routes, two questions (GH #398)

Neither is "complete order history", and each **says what it cannot contain in its own
`coverage` block**, so a consumer cannot mistake one for the other.

### `GET /accounts/{account}/orders?broker=…` — the engine's store

Every order **this engine** routed for the account, newest first, with fills.

| param | meaning |
|---|---|
| `broker` | required, as on every account route |
| `since`, `until` | ISO timestamps **with an offset** — a naive timestamp is **refused (422)**, never assumed UTC |
| `limit` | 1-500, default 100 |
| `cursor` | pass back `next_cursor` unchanged. It is opaque and URL-safe |

`coverage.cannot_contain` names what is missing: orders placed **by hand** or by any other
application, and orders routed by the **other node**. `coverage.complete_since` is this node's
first stored order. Every row carries `broker` — **a `sim` row is not a real order**.

### `GET /accounts/{account}/venue-orders?broker=…` — the venue's own list

Every row the broker lists for the account, in **any state** and from **any origin**. The venue
lists by account, not by client, so this is the one route that can show an order the engine did
not send. Unlike `/open-orders` it keeps filled and cancelled rows. None is dropped: a field the
engine cannot map is `null`, and the venue's own `venue_status` / `venue_status_code` /
`placed_at` are always carried verbatim.

`state` is resting · partially_filled · filled · cancelled · rejected · expired · unknown. It is
derived from the venue's terminal words **plus** the matched and remaining counters, because the
reconciler's coarse classifier calls a fully matched order "resting".

**Day scope differs by broker, and `coverage` says which, with its evidence:**

| broker | `day_scope` | evidence |
|---|---|---|
| liberator | `today_only` | **OBSERVED** — the venue keeps no order history (umbrella `docs/reference/liberator-account-reads.md` §9.3) |
| streaming_pro | `not_established` | **NOT MEASURED** ([[TK-0459]]) — treat as today only until it is |

Both routes are gated exactly like the other account reads (stage ladder + EH6), and both accept
the read-only key.

## `GET /accounts/{account}/order-path?broker=&market=&symbol=&price=` — is the order path open? (GH #468)

One **no-order** call on the venue's order path, and what it found. It exists because on
2026-11-24 the AWS Liberator bridge refused TFEX places with HTTP 403 while the engine's session
heartbeat and every read stayed healthy. Nothing the engine served could tell
"the session is alive" from "the venue will take an order".

```bash
curl "http://localhost:8400/accounts/70000007/order-path?broker=liberator&market=TFEX&symbol=S50Z26&price=700" \
  -H "X-API-Key: <key>"
```

```json
{
  "broker": "liberator", "market": "TFEX", "symbol": "S50Z26", "price": "700",
  "ok": true, "outcome": "ok", "bridge_status": 200,
  "message": "TFEX order pre-place calculation completed successfully",
  "retried": false, "first_outcome": null, "first_bridge_status": null,
  "places_no_order": true, "checked_at": "2026-11-25T09:30:00.123456+00:00", "cached": false
}
```

**What it calls.** The engine builds a real `NormalizedOrder` (BUY · LIMIT · DAY; 100 shares on
SET, one contract `OPEN` on TFEX) and sends **the exact body a place would send** to the bridge's
fee calculation, `POST order/pre-place/{set|tfex}`. That call places nothing. It goes through the
same cached bridge session as a place (liberator-trading-api `3b7632d`,
`tfex_order_service.pre_place_order` and `place_order` both use `_get_session()`).
`symbol` and `price` are the caller's: an instrument hard-coded in the engine would expire with
its contract month. Pick a price the venue will calculate for, normally near the market.

**Probe the market the node actually holds an account for.** The nodes are not symmetric: HOME has
no derivatives account at all, only a SET cash account (umbrella `CLAUDE.md`, one broker account
per node). A TFEX probe on HOME can never answer `ok`, and that is not a fault. Probe SET there,
and TFEX on AWS.

**HTTP 200 whenever it ran. The finding is `outcome`**, and `ok` is `outcome == "ok"`:

| `outcome` | means | the bridge said |
|---|---|---|
| `ok` | the venue calculated this order | 200, `errorCode` 0 |
| `refused` | the venue or its edge refused **this node's session** | 401/403; or (from `3b7632d`) a 503 marked as a refused session |
| `venue_rejected` | the venue answered and declined this order | 400 "calculation failed", or 200 with a non-zero `errorCode` |
| `input_rejected` | the body was refused before the venue saw it | 422, 400 with `error: validation_error`, or a price the wire cannot carry |
| `unreachable` | no connection to the bridge, or the bridge could not reach the venue | a connection failure, 500, or any other 503 |
| `unexpected` | an answer none of the above describes | anything else, including 400 with `error_code` `json_parse_error`, `validation_error` or `upstream_response_invalid` (the bridge could not read the venue's answer; the last is a proposed rename of the second) |
| `unsupported` | this broker has no call on its order path that places nothing | — (Streaming Pro, sim) |

⚠️ **`ok` means the fee calculation was accepted. It does not prove a place would be.** The
evidence is one incident on AWS, 2026-11-25: pre-place answered 401 at 10:04:05 BKK while places
were refused, and 200 after the 10:04:34 bridge restart (GH #468). A refused pre-place tracked a
refused place. Whether an accepted pre-place means an accepted place had not been shown by a real
order when this was written.

🔑 **One retry, only after the bridge DISCARDS a refused session.** A bridge at `3b7632d` or later
answers a refused session with a 503 and retires it, so the next call builds a fresh one; its
message says *"RETRY THE CALL FIRST"*. The probe does exactly that, once. After the HOME bridge
deploy of 2026-11-25 ~18:04 BKK, the first venue call of two separate bridge services (one of them
this pre-place) was refused on a new session and the rebuilt session worked, 2 of 2 (GH #468): a
single call would have read `refused` on a healthy bridge.

| answer | what it means |
|---|---|
| `ok`, `retried: false` | the order path was open |
| `ok`, `retried: true`, `first_outcome: "refused"` | the bridge discarded a refused session and a fresh one was accepted: it recovered by itself |
| `refused`, `retried: true` | refused twice: the session the bridge discarded, and the fresh one. This says WHAT happened, not why. 🔴 Do not read a cause into it: first calls on new sessions were refused 2 of 2 times after the 2026-11-25 HOME restart, so two refusals in a row is not rare. The bridge's own message offers one hypothesis (the account logged in elsewhere) as a hypothesis. A re-login fires a real SMS OTP, so read the bridge's log before choosing a remedy |
| `refused`, `retried: false` | a 401/403. An older bridge (before `3b7632d`) discards nothing, so asking again would hit the same session; no retry |

Retrying is safe only because pre-place places nothing. Nothing on a place path retries.

🔶 **How a refused session is recognised on a 503 is temporary.** At `3b7632d` the bridge's endpoints
drop the exception's `details`, so the engine matches the bridge's sentence *"SESSION was
refused"*. liberator-trading-api PR #65 (opened 2026-11-25) carries `details.classification`
through. The engine already accepts `classification == "edge_block"`, so no engine change is
needed when it ships. The sentence match stays until #65 is **running** on every node: a merge or
a pin bump deploys nothing on a containerised bridge.

**Bounded.** At most **one probe per `(broker, account, market)` per
`EXECUTION_ENGINE_ORDER_PATH_PROBE_MIN_INTERVAL_SECONDS`** (default 30), and one probe is at most
**two** venue calls (the retry above). A repeat inside the window gets the last answer with
`cached: true` and the original `checked_at`, whatever its `symbol` or `price`. Concurrent callers
share one probe. The bound is per process.

**Never touches the breaker.** A refused probe is a finding to report, and three breaker failures
mass-cancel.

Gated exactly like the other account reads (stage ladder + EH6), so an undeclared account is
refused before any venue call. It accepts the read-only key. The answer does not echo the account,
and digit runs of six or more in a venue message are masked.

**Not proxied by the gateway.** The gateway proxies an explicit list of execution routes, and this
route, like the two order-history routes above, is not on it. Call the engine directly: on HOME
`:8400`, on AWS over the internal `execution-read-aws` network.

## Credentials

These routes accept **either** the full `EXECUTION_ENGINE_API_KEY` **or** the optional read-only
`EXECUTION_ENGINE_READ_API_KEY` ([[TK-0442]] engine layer, GH #395). The read key is accepted
**nowhere else**: every order write and the kill-switch answer 403 `read_key_forbidden` to it. A
monitor should hold only the read key.

## ➡️ Positions ARE served — this section used to say otherwise

↻ *This section was headed "Not served here" and said `get_positions` raises 501. That stopped being
true when positions shipped ([[TK-0396]], then the money block in [[TK-0480]]).* `GET
/accounts/{account}/positions` is served for both brokers. The 501 survives only for a row whose
schema has never been observed — e.g. a Streaming Pro TFEX account that actually holds something —
where the engine refuses rather than inventing field names.

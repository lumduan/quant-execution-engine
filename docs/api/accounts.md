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

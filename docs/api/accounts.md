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
| `liberator_positions_uncaptured` | 501 | positions are not implementable yet — [[TK-0396]] |
| `public_mode` | 403 | owner mode only; these expose real account financials |

## Not served here

**Positions.** `get_positions` raises 501: `POST /va/portfolio` answers `result.{list, stock}` and
**neither array has ever been observed non-empty**, so the element schema has never been captured.
Adding a route would not fix it — the parse would have to invent field names, which is how the
`buying_power=0` bug was made. Tracked on [[TK-0396]].

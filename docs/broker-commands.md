# Broker commands — the unified surface, for strategy authors

**Read this before writing integration code.** It covers every command a strategy needs — buy, sell,
cancel, amend, open orders, positions, account balance — across **both real brokers**, and says
plainly which of them you can call **today**.

> **Verified 2026-08-21 against the running service and the two bridge repos**
> (`lumduan/liberator-trading-api` @ `76d925e`, `lumduan/settrade-streaming-api` @ `c3a987c`),
> not against older docs. Where a statement came from a doc rather than code, it says so.

Per-endpoint detail lives in [`api/`](api/). This page is the cross-cutting guide those pages lack.

---

## 1 · The one thing to understand first

**You never call a broker. You call this engine, and name the broker in a field.**

```
your strategy ──POST /orders {"broker": "liberator",     …}──┐
                                                              ├──► quant-execution-engine ──► the right bridge
your strategy ──POST /orders {"broker": "streaming_pro", …}──┘
```

One contract (`NormalizedOrder`), one route, one auth scheme. Adding a broker is an adapter, not a
change to your code. `broker` is just a field — the same request body works for all three values.

Legal values: **`liberator`** · **`streaming_pro`** · **`sim`**.

> ⚠️ **`settrade` is not a broker value.** The Settrade Open-API adapter (broker-023) was **deleted
> 2026-07-18**. If you find `settrade` in any document or example, that document is stale — please
> report it. `streaming_pro` is the only Settrade-family path and it is the *retail Streaming Pro
> bridge*, a different thing from the Open API.

---

## 2 · What you can call today — the honest matrix

Every cell is **LIVE** (callable now) or **DESIGNED-ONLY** (the adapter implements it; no route
exists, so you cannot reach it).

| Command | liberator | streaming_pro | sim | How you call it |
|---|---|---|---|---|
| **Place order** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `POST /orders` |
| **Cancel order** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `DELETE /orders/{client_order_id}` |
| **Amend order** | 🟢 LIVE¹ | 🟢 LIVE¹ | 🟢 LIVE | `PATCH /orders/{client_order_id}` |
| **Order status** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `GET /orders/{client_order_id}` |
| **Order updates (stream)** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `GET /orders/stream` (SSE) |
| **Capabilities** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `GET /capabilities` |
| **Open orders** | 🔴 DESIGNED-ONLY | 🔴 DESIGNED-ONLY | 🔴 DESIGNED-ONLY | — no route |
| **Positions** | 🔴 DESIGNED-ONLY | 🔴 DESIGNED-ONLY | 🔴 DESIGNED-ONLY | — no route |
| **Account balance** | 🔴 DESIGNED-ONLY | 🔴 DESIGNED-ONLY | 🔴 DESIGNED-ONLY | — no route |

¹ **Amend is emulated, not native — see §5.** No real broker supports in-place amend.

**Why the bottom three are DESIGNED-ONLY and not "missing":** `get_open_orders`, `get_positions` and
`get_account` are **implemented in all three adapters**, and the two real adapters genuinely call
their bridges. They simply have **no HTTP route and no caller** anywhere in the service. The work to
expose them is two routes, not a capability — see §7 and [[TK-0396]].

---

## 3 · Every LIVE endpoint

Both path forms are equivalent; use whichever your deployment reaches.

| | native | via the gateway prefix |
|---|---|---|
| all routes below | `http://quant-execution-engine:8000/…` | `…/api/v2/engines/execution/…` |

**Auth:** `X-API-Key` on everything. Writes (`POST`/`DELETE`/`PATCH`) additionally require the engine
to be in **owner mode** (`EXECUTION_ENGINE_PUBLIC_MODE=false`); in public mode they return
`403 public_mode`.

### `POST /orders` — place

Body is the frozen `NormalizedOrder`:

| field | type | notes |
|---|---|---|
| `client_order_id` | string | **you generate it** (UUIDv4), 8–64 chars. Your idempotency key |
| `broker` | `sim` \| `liberator` \| `streaming_pro` | |
| `account` | string | broker account number |
| `market` | `SET` \| `TFEX` | **independent of `broker`** — see §4 |
| `symbol` | string | |
| `side` | `BUY` \| `SELL` | |
| `order_type` | `MARKET` `LIMIT` `STOP` `STOP_LIMIT` `ICEBERG` `MTL` `ATO` `ATC` | **not all valid for all brokers — §6** |
| `price` | decimal-as-string | required for LIMIT |
| `stop_price` | decimal-as-string | required for STOP / STOP_LIMIT |
| `quantity` | int > 0 | ⚠️ **see the streaming_pro cap in §5** |
| `display_qty` | int > 0 \| null | ICEBERG |
| `tif` | `DAY` \| `IOC` \| `FOK` \| `GTC` | **not all valid for all brokers — §6** |
| `position_effect` | `OPEN` \| `CLOSE` \| null | **required for TFEX, forbidden for SET** |

Returns `NormalizedOrderResult`: `client_order_id`, `broker_order_id`, `broker`, `status`,
`engine_state`, `filled_qty`, `remaining_qty`, `avg_fill_price`, `reject_reason`, `created_at`,
`updated_at`.

**Resubmitting the same `client_order_id` is safe** — you get the original ack back, not a second
order.

### `DELETE /orders/{client_order_id}` — cancel
### `GET /orders/{client_order_id}` — status
### `PATCH /orders/{client_order_id}` — amend → **read §5 first**
### `GET /orders/stream` — SSE, `Last-Event-ID` reconnect, `strategy_id` filter
### `GET /capabilities` — what each `(broker, market)` accepts. Query it; don't hardcode §6.

---

## 4 · Acting on both brokers — the worked example

**One call targets one broker.** To act on both, issue **two calls that differ only in `broker`**.

```jsonc
// call 1
POST /orders
{ "client_order_id": "8f14e45f-…-a1", "broker": "liberator",
  "account": "7041257", "market": "SET", "symbol": "PTT",
  "side": "BUY", "order_type": "LIMIT", "price": "35.50",
  "quantity": 100, "tif": "DAY" }

// call 2 — same shape, different broker AND a different client_order_id
POST /orders
{ "client_order_id": "c9f0f895-…-b2", "broker": "streaming_pro",
  "account": "053209", "market": "SET", "symbol": "PTT",
  "side": "BUY", "order_type": "LIMIT", "price": "35.50",
  "quantity": 1, "tif": "DAY" }
```

🔴 **Four things that will bite you:**

1. **Each call needs its OWN `client_order_id`.** Reusing one is an idempotent resend — you would
   get the first order back and the second broker would never be touched.
2. **There is NO cross-broker atomicity.** Call 1 can fill while call 2 rejects. If you need both or
   neither, *you* implement the unwind. The engine will not do it for you.
3. **The accounts differ per broker** — they are different institutions, not aliases.
4. **`quantity` may need to differ** — note `1` on the streaming_pro leg. See §5.

---

## 5 · Per-broker gotchas — attributed, never symmetric

### 🔴 streaming_pro: every order is hard-capped at **quantity 1**

The bridge enforces `_MAX_ORDER_VOLUME = 1`, described in its own source as a *"hard,
NON-env-overridable"* Phase-3 double-safety. Anything larger is rejected `422` **at the bridge**.

⚠️ **You cannot discover this from `/capabilities`** — the capability cells carry no quantity field
at all. And **you cannot discover it by testing**, because at `sim` and `paper` the order never
reaches the bridge: a `quantity: 100` streaming_pro order fills perfectly in both. **It first fails
at `micro_live`.** Plan for it rather than meeting it.

*(The bridge comment says the cap lifts once the engine's PTRM caps front it. The engine's PTRM
default is `1000` and does front it, so this looks like a temporary measure outliving its condition —
but it is `session:sp-research`'s to change, and until they do, the cap is 1.)*

### 🔴 Amend is cancel-then-replace on BOTH real brokers, for different reasons

Only `sim` declares `native` amend. In code: *"after the broker-023 removal no REAL broker declares
`native`"*.

| broker | why |
|---|---|
| **liberator** | the bridge has **no amend route at all** — zero `PATCH` handlers, no amend/modify/replace method anywhere |
| **streaming_pro** | a `/order/change` route **exists but always returns `501`** — its handler body is a bare `raise CapturePendingError(...)` with no upstream call. The bridge self-declares `native_amend: false` |

**Consequences you must design around:** the amend is **non-atomic** — the old order is cancelled and
a new one placed. You **lose queue priority**, and there is a **window with no resting order**. A
`cancel_replace` amend returns the **replacement** `client_order_id`, so supply
`new_client_order_id`; a `native` (`sim`) amend keeps the same id and forbids that field.

### 🔴 streaming_pro: positions AND balance are **SET-only**

The bridge's account service has **zero** references to `seosd` (the TFEX segment). There is no TFEX
route — not one returning empty, it does not exist.

⚠️ Wider than previously recorded: it is **not only positions**. `account-info` (balance /
buying-power) is equally SET-only. Any future `/positions` or `/account` response must mark this
per-broker rather than implying symmetric coverage.

### liberator: the typed portfolio/profile models are **not populated**

`portfolio_service` returns `data=None` unconditionally and passes the venue payload through as an
untyped `raw_response`. The rich `PortfolioPosition` / `PortfolioSummary` models are dead code.
Whether a given account's portfolio covers SET, TFEX or both **cannot be determined from the bridge
source** and must be verified empirically.

### liberator: the PIN you send is discarded

Place/cancel bodies require a well-formed 6–10 digit `pin`, and the service **overwrites it** with
`self._settings.liberator_pin` from the bridge's own environment. It must be well-formed; it does not
need to be *correct*. **The engine never sends a PIN** — this only matters if you read the bridge
directly.

*(streaming_pro is cleaner here: no request model has a `pin` field at all; the bridge stamps it.)*

### ⚠️ Do not read `app/core/api_endpoints.py` in the liberator bridge

It looks like an authoritative endpoint map and is **dead code** — nothing imports it, and the paths
it lists (`/trading/orders`, `/account/balance`) do not exist. A trap, not a source.

---

## 6 · Capability differences — query, don't hardcode

From `contracts/capabilities.py`, cross-checked against the live `GET /capabilities`:

| | order types | TIFs | amend |
|---|---|---|---|
| **liberator · SET** | MARKET · LIMIT · ICEBERG · MTL · ATO · ATC — **no STOP** | all four | cancel_replace |
| **liberator · TFEX** | MARKET · LIMIT · STOP · STOP_LIMIT · ICEBERG — **no ATO/ATC/MTL** | all four | cancel_replace |
| **streaming_pro · SET** | MARKET · LIMIT only | **DAY only** | cancel_replace |
| **streaming_pro · TFEX** | MARKET · LIMIT only | **DAY only** | cancel_replace |
| **sim · both** | all eight | all four | **native** |

🔑 **Liberator's two markets are not the same** — SET has the auction types, TFEX has the stops.
streaming_pro's cells are deliberately conservative and expand as combinations are live-verified.
The router rejects an unsupported combination up front with `capability_unsupported`.

---

## 7 · DESIGNED-ONLY — positions, balance, open orders

**Not built. Do not code against this section yet.** Recorded so the shape is agreed before it is.

```
GET /positions?account=<acct>&broker=<broker>   ->  [ {account, market, symbol, net_qty} ]
GET /account?account=<acct>&broker=<broker>     ->  { account, buying_power }
```

These return the **existing contracts unchanged** (`adapters/base.py`), which means, plainly:

* ❌ no cost basis, no market value, no unrealised P&L — **`net_qty` only**
* ❌ no cash / equity / margin split — **`buying_power` only**
* ❌ **streaming_pro TFEX would be empty**, per §5
* ❌ liberator coverage unverified, per §5

If your strategy needs P&L or a cash breakdown, say so before this is built — the contracts would
have to grow, and that is a bigger change than the two routes.

---

## 8 · Stages — what they mean to you as a caller

| stage | what happens to your order |
|---|---|
| `sim` | `SimAdapter`. `broker` is accepted and recorded but **ignored for routing** |
| `paper` | **placements are intercepted to `SimAdapter`** — you get a `SIM-…` `broker_order_id`; reads pass through to the real broker |
| `micro_live` | real routing, PTRM-capped. **Where §5's streaming_pro quantity cap first bites** |
| `live` | **hard reject** — `stage_rejected`, always |

**A `SIM-` prefix on `broker_order_id` means nothing reached a venue** — the reliable signal, whatever
the stage says.

---

## Related

[`api/orders-submit.md`](api/orders-submit.md) · [`api/orders-cancel.md`](api/orders-cancel.md) ·
[`api/orders-amend.md`](api/orders-amend.md) · [`api/orders-get.md`](api/orders-get.md) ·
[`api/orders-stream.md`](api/orders-stream.md) · [`api/capabilities.md`](api/capabilities.md) ·
[`architecture/adapters.md`](architecture/adapters.md) ·
[`../.claude/knowledge/capability-matrix.md`](../.claude/knowledge/capability-matrix.md)

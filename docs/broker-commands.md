# Broker commands — the unified surface, for strategy authors

**Read this before writing integration code.** It covers every command a strategy needs — buy, sell,
cancel, amend, open orders, positions, account balance — across **both real brokers**, and says
plainly which of them you can call **today**.

> **Verified 2026-08-21 against the running service and the two bridge repos**
> (`lumduan/liberator-trading-api` @ `76d925e`, `lumduan/settrade-streaming-api` @ `c3a987c`),
> not against older docs. Where a statement came from a doc rather than code, it says so.
>
> 🔴 **Partially superseded 2026-08-24 by live measurement against both venues.** The liberator
> balance/positions claims made under this banner were verified *against the bridge source*, which
> is itself documented with fabricated examples — so "verified" meant self-consistent, not correct.
> Corrections are marked inline below rather than silently edited. Captured wire formats:
> [`docs/reference/liberator-account-reads.md`](../../docs/reference/liberator-account-reads.md).

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

> ✅ **DEPLOYED TWICE on 2026-08-27** — `10:23 UTC` (`c2b8962`, the balance + open-orders routes)
> and `15:45 UTC` (`5d7ab86`, the SP **TFEX** balance front). The fourth state
> (`MERGED-NOT-DEPLOYED`, added earlier the same day) is **retired**: every cell below is callable
> now. Verified through the real deployed route on real accounts, **not** from the merge — all four,
> all HTTP 200:
>
> | account | broker | type | buying_power |
> |---|---|---|---|
> | `70000002` | liberator | cash (SET) | **50,000.11** |
> | `70000007` | liberator | derivative (TFEX) | **13,000.22** |
> | `0500007` | streaming_pro | cash (SET) | **38,000.33** |
> | `0500009` | streaming_pro | derivative (TFEX) | **10,000.44** |
>
> The retired state is kept in the history below because the distinction it drew — *merged is not
> callable* — is the one this table exists to hold.

| Command | liberator | streaming_pro | sim | How you call it |
|---|---|---|---|---|
| **Place order** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `POST /orders` |
| **Cancel order** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `DELETE /orders/{client_order_id}` |
| **Amend order** | 🟢 LIVE¹ | 🟢 LIVE¹ | 🟢 LIVE | `PATCH /orders/{client_order_id}` |
| **Order status** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `GET /orders/{client_order_id}` |
| **Order updates (stream)** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `GET /orders/stream` (SSE) |
| **Capabilities** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `GET /capabilities` |
| **Open orders** | 🟢 LIVE | 🟢 LIVE | 🟢 LIVE | `GET /accounts/{account}/open-orders?broker=` |
| **Positions** | 🔴 **DESIGNED-ONLY**² | 🔴 DESIGNED-ONLY (SET only) | 🔴 DESIGNED-ONLY | — no route |
| **Account balance** | 🟢 **LIVE** — SET **+ TFEX**³ | 🟢 **LIVE** — SET **+ TFEX**⁴ | 🟢 LIVE | `GET /accounts/{account}?broker=` |

¹ **Amend is emulated, not native — see §5.** No real broker supports in-place amend.

² ➡️ **PROMOTED 2026-08-28: liberator positions moved NOT IMPLEMENTED → DESIGNED-ONLY.** The adapter
now works; only the route is missing.

The third label still exists and still matters — *NOT IMPLEMENTED* means the adapter does not work
either, so building a route would expose a 501 rather than data. Liberator sat there from 2026-08-24
because the `result.stock[]` element schema had never been observed on a populated response.

**The operator opened real SET and TFEX positions and the capture was taken 2026-08-28.** The schema
is recorded in the umbrella's private `docs/reference/liberator-account-reads.md` §2.2 (values live
there and deliberately not here — this repo is public). `LiberatorAdapter.get_positions` parses it,
every field tagged OBSERVED, seven guards mutation-proven.

🔑 **The four-month refusal was vindicated, which is why it is worth recording rather than quietly
overwriting.** The ten field names recoverable from the venue's web client were flagged as a *lower
bound*; the venue actually sends **17** on a TFEX row, and one of the ten — `optVal` — **does not
exist at all**. Implementing against them in August would have shipped a schema that was already
known to be incomplete and was in fact also wrong.

⚠️ **What remains is genuinely just the route** — and it is a deliberate non-decision, not an
oversight: `StreamingProAdapter.get_positions` is still SET-only, so adding `GET
/accounts/{account}/positions` today would publish a surface that answers correctly for one broker
and partially for the other. That asymmetry wants its own call.

³ ✅ **Liberator balance covers SET *and* TFEX, and TFEX is NOT a separate branch — confirmed
2026-08-27.** `get_account` issues **one** request to a constant path (`_PROFILE_PATH`), receives
**both** accounts in a single `accounts[]` array, and selects by string equality on `accountNo`
(`liberator/adapter.py:330-337`). There is **no market parameter and no per-market request**, so a
TFEX balance cannot be an untested request-level branch. Asserted, not argued: a route-level test
drives both accounts and checks the transport saw the **same path both times**.

The one genuinely market-dependent line is the margin block (`_account_info`, DERIVATIVE-only), and
the live payload proves both readings at once — the CASH entry **omits** `totalMr`/`totalMm` (→
`null`) while the DERIVATIVE entry **reports** them as `0` (→ `0`). Same function, real bytes,
`None` and `0` not conflated. Live values 2026-08-27: `70000002` → 50,000.11 (cash),
`70000007` → 13,000.22 (derivative, equity 13,000.22, IM/MM 0).

✅ **The end-to-end hop is now proven** (2026-08-27 15:47 UTC): both Liberator accounts were read
*through the deployed route, on the AWS node*, which is where those accounts live — so the
one-account-per-node rule is respected rather than worked around. This paragraph previously said the
hop *"cannot be run under the freeze"*; the freeze lifted, and it was run.

⁴ ✅ **SP balance now covers TFEX too (2026-08-27, PR #49) — and it resolves the market by ASKING
THE VENUE, never by reading the account number.** The two SP fronts are mutually exclusive: SET
answers on `fis`/`account-info`, TFEX on `seosd`/`tfex/account-info`. `get_account` tries SET, and
**only** if SET does not return a balance does it try TFEX; if neither front answers it **raises**
`StreamingProAccountUnavailable` rather than reporting a number.

🔴 **Why "ask the venue" is not pedantry here: `0500007` (SET) and `0500009` (TFEX) differ by ONE
DIGIT.** Any rule that infers the market from the number is one typo away from answering a request
with the wrong market's book. And the refusal cannot be detected from the status line — **the venue
returns HTTP 200 with the error in the body** (`{"code": "GWD-03", "message": "UserAccount not
found…"}`), so an adapter keyed on `status_code == 200` would read a refusal as a success and, if it
defaulted the missing balance, report a **confident zero for an account it could not read**. Both
behaviours are pinned by tests built on the **verbatim captured bodies**, including a positive
control asserting a SET account never falls through to the TFEX front.

Live TFEX values 2026-08-27 (`0500009`): buying_power/equity **10,000.44** (`excessEquity`),
credit_line 50,000, IM/MM **`0.0` — reported, not absent.** This is the mirror of Liberator's case
in ³: there the CASH entry *omits* the margin fields (→ `null`) while the DERIVATIVE entry *reports*
them as `0`; here TFEX reports `0`. **Collapsing either direction is the bug**, and a test asserts a
reported zero never becomes `null`.

⚠️ **`get_positions` is still SET-only** — it hardcodes `Market.SET` and its docstring still says
*"TFEX is a follow-up"*. The positions row above stays DESIGNED-ONLY for that reason.

⁵ ✅ **RESOLVED for SP-SET 2026-08-27** — `0500007` was added to the AWS declaration after verifying
it is AWS's own SBITO/033 account (the bridge's `account-info` reports `brokerId 33`, `accountNo
0500007`), so the SP balance read returns 200 rather than 409. ⚠️ The underlying property is unchanged
and [[TK-0443]] stays open: the guard is broker-agnostic, so **any SP account that is NOT declared is
still refused**. What follows is the original finding.

🔴 **EH6 blocked Streaming Pro entirely on the AWS node**, independent of any of the above.
`EXECUTION_ENGINE_REAL_ROUTING_ACCOUNTS` lists two **Liberator** accounts, and
`assert_may_route_real` is broker-agnostic — so any real SP routing, **order or read**, returns
409 `real_routing_not_authorized`. A config gap, not a code gap. See [[TK-0443]].

**Why the bottom three are not simply "missing":** `get_open_orders`, `get_positions` and
`get_account` exist on all three adapters, and they have **no HTTP route and no caller** anywhere in
the service.

> ⛔ **CORRECTED 2026-08-24 (PR #38) — "implemented in all three adapters" is NO LONGER TRUE.**
> `LiberatorAdapter.get_positions` raises rather than returning data. The sentence above used to say
> *"implemented in all three adapters, and the two real adapters genuinely call their bridges"*, and
> a reader could reasonably conclude that shipping two routes would deliver positions. **It would
> not.** See footnote ² and §7.

> 🔴 **CORRECTED 2026-08-24 — "the work to expose them is two routes, not a capability" was TRUE for
> streaming_pro and FALSE for liberator.** Measured live: `LiberatorAdapter.get_account` calls
> `portfolio/get`, **an endpoint that carries no balance field in any shape**, and returns
> `buying_power=0` for accounts holding real five-figure balances. Shipping the route today ships a
> confident zero. "The two real adapters genuinely call their bridges" is literally true and
> materially misleading — the call lands on a payload that cannot answer the question.
>
> The endpoint ruling and its status are on [[TK-0396]]; the captured wire formats are in
> [`docs/reference/liberator-account-reads.md`](../../docs/reference/liberator-account-reads.md)
> (umbrella).
>
> 🔴 **Reconciled again 2026-08-24 (later the same day) after PRs #37/#38 shipped.** Three things
> changed *after* the correction above was written: `LiberatorAdapter.get_positions` now **raises**,
> `AccountInfo` **gained** the cash/equity/margin split it was described as lacking, and `micro_live`
> acquired a **new start-up requirement** (EH6). All three are marked inline below.

---

## 3 · Every LIVE endpoint

Both path forms are equivalent; use whichever your deployment reaches.

| | native | via the gateway prefix |
|---|---|---|
| all routes below | `http://<engine-host>:8000/…` | `…/api/v2/engines/execution/…` |

> 🔴 **Corrected 2026-08-24 — this table said `http://quant-execution-engine:8000`, which resolves on
> HOME and NOT on the AWS node.** The AWS container's network aliases are **`execution-engine`** and
> `quant-execution-engine-aws` (measured). Copying the old literal gets a DNS failure that reads like
> the engine being down.
>
> **How to address it depends on how YOUR caller is networked — get this right before debugging
> anything else:**
>
> | your caller | use | why |
> |---|---|---|
> | a container on the **same docker network** (`capture-redundancy-aws` on AWS) | `http://execution-engine:8000` | service-name DNS on that bridge |
> | a container on **`network=host`** | ✅ **`http://localhost:8400`** — measured HTTP 200 | it shares the host's loopback, so the `127.0.0.1` bind is reachable |
> | anything **not on that bridge and not host-networked** | ⛔ neither works | `execution-engine` does not resolve, and `:8400` is bound to `127.0.0.1` — **not** `0.0.0.0` |
>
> ⚠️ **The two are mutually exclusive, and the host-networked case is counter-intuitive:** a bind to
> `127.0.0.1` normally means "containers cannot reach this", and for a *bridge*-networked container it
> does. A **host**-networked one reaches it fine. If `execution-engine:8000` fails for you, you are
> probably host-networked — use `localhost:8400`, do not conclude the engine is unreachable.

**Auth:** `X-API-Key` on everything — **omitting it returns `401` on the AWS node** (verified). Writes
(`POST`/`DELETE`/`PATCH`) additionally require the engine to be in **owner mode**
(`EXECUTION_ENGINE_PUBLIC_MODE=false`); in public mode they return `403 public_mode`.

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
  "account": "70000012", "market": "SET", "symbol": "PTT",   // 8 digits — see the note below
  "side": "BUY", "order_type": "LIMIT", "price": "35.50",
  "quantity": 100, "tif": "DAY" }

// call 2 — same shape, different broker AND a different client_order_id
POST /orders
{ "client_order_id": "c9f0f895-…-b2", "broker": "streaming_pro",
  "account": "050000", "market": "SET", "symbol": "PTT",
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

### ➡️ streaming_pro: balance is now SET **+ TFEX**; positions are still SET-only

> 🕐 **This section's original claim is kept as history because it was TRUE when written, and
> because the reasoning it records still applies to positions.** It said: *"The bridge's account
> service has **zero** references to `seosd` (the TFEX segment). There is no TFEX route — not one
> returning empty, it does not exist,"* and that the limit covered balance as well as positions.
>
> ➡️ **SUPERSEDED FOR BALANCE on 2026-08-27** by `session:sp-research`, who added the `seosd` front
> to the bridge (`efdb6c5`, bridge PR #24 / issue #236). `GET /api/v1/tfex/account-info` exists now
> and returns a real body. This engine consumes it as of PR #49 — see footnote ⁴ in §2.

**Standing today.** The two fronts are real and mutually exclusive — `fis`/`seos` serve equity,
`seosd` serves derivatives — so *which front answers* is the market discriminator, and the account
number is not. Balance uses both fronts. **Positions still use only `fis`**: the adapter hardcodes
`Market.SET`, so the SET-only limit below is unchanged for that call.

⚠️ The original warning survives its own supersession and is worth restating: a `/positions` or
`/account` response must mark coverage **per-broker and per-market**, never imply symmetry. Balance
and positions now differ *within a single broker* — which is exactly the asymmetry that sentence was
written to stop anyone assuming away.

### liberator: portfolio and profile are **two different endpoints with two different jobs**

`portfolio_service` returns `data=None` unconditionally and passes the venue payload through as an
untyped `raw_response`. The rich `PortfolioPosition` / `PortfolioSummary` models are dead code.
*(Both still true — and together they are the mechanism behind the zero above.)*

> ✅ **ANSWERED 2026-08-24 — the old closing sentence said market coverage "cannot be determined from
> the bridge source and must be verified empirically". It was verified empirically.** There is **no
> SET/TFEX read split at all**: one route, `POST /va/portfolio`, body `{accountNo}`, no market
> parameter. **Market is a property of the account number**, and `/va/profile`'s `type` field is the
> only place that mapping is stated.
>
> | I want | Call |
> |---|---|
> | balance / buying power / margin | `GET /va/profile` → `result.accounts[]` — the **only** source |
> | holdings | `POST /va/portfolio` → `result.{list, stock}` |
> | "is this account authorized?" | `raw_response.errMsg` — 🔴 **never** `success`, which is `true` even for a refused account |
>
> ⚠️ **Liberator accounts are 8 digits, `<login><suffix>`** — suffix `2` = CASH BALANCE (SET), `7` =
> DERIVATIVE (TFEX). A bare login is **not** an account, and the portfolio read *accepts* one and
> returns an authorized-looking empty. The zero-padded 10-digit form is **rejected**. Full grammar,
> both captured envelopes, and the latency comparison:
> [`docs/reference/liberator-account-reads.md`](../../docs/reference/liberator-account-reads.md).

### liberator: the PIN you send is discarded

Place/cancel bodies require a well-formed 6–10 digit `pin`, and the service **overwrites it** with
`self._settings.liberator_pin` from the bridge's own environment. It must be well-formed; it does not
need to be *correct*. **The engine never sends a PIN** — this only matters if you read the bridge
directly.

*(streaming_pro is cleaner here: no request model has a `pin` field at all; the bridge stamps it.)*

### ⚠️ Do not read `app/core/api_endpoints.py` in the liberator bridge

It looks like an authoritative endpoint map and is **dead code** — nothing imports it, and the paths
it lists (`/trading/orders`, `/account/balance`) do not exist. A trap, not a source.

⚠️ **Do not take the wrong lesson from that.** "`/account/balance` does not exist" is true of *that
file*; a balance surface **does** exist — `GET /va/profile` → `result.accounts[]`. And it is not the
only fabrication in that repo: its portfolio/profile docs and fixtures carry three mutually
contradictory shapes, an `AAPL` position, and an account form the venue rejects. Every one is
enumerated under "FABRICATED — do not copy" in
[`docs/reference/liberator-account-reads.md`](../../docs/reference/liberator-account-reads.md).

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

> 🔴 **`adapter_installed` — read this before trusting it, and note what it used to be.**
>
> **Until 2026-08-25 it was a hardcoded `True`** in the static matrix: a *build-time* constant
> wearing a *deployment-fact* name. It meant *"an adapter class exists in this codebase"* and was
> reasonably read as *"this node can route it"*. On the AWS node it reported
> `liberator adapter_installed=True` while the node held **no Liberator credential** — where a
> `micro_live` order is `StageRejected`, not a fill. `session:cash-carry` hit exactly that while
> planning a gate, **following this section's own "query, don't hardcode" advice.**
>
> ✅ **It is now computed per request from the constructed runtime** — `true` only if this
> deployment can actually route that broker right now.
>
> ⚠️ **A `false` means "not routable on THIS node as configured", not "missing from the build".**
> At `sim`/`paper` the real brokers read `false` by design: no real runtime is constructed without
> owner mode plus credentials. The response's `stage` field is the context that disambiguates it,
> and the sibling `brokers` object carries breaker/session state for whatever *is* constructed.

---

## 7 · DESIGNED-ONLY — positions, balance, open orders

**Not built. Do not code against this section yet.** Recorded so the shape is agreed before it is.

```
GET /positions?account=<acct>&broker=<broker>   ->  [ {account, market, symbol, net_qty} ]
GET /account?account=<acct>&broker=<broker>     ->  { account, buying_power }
```

Where each stands, as of 2026-08-24:

* ✅ **`AccountInfo` NOW CARRIES the cash/equity/margin split** (PR #37). `account_type` plus
  `buying_power`, `cash_balance`, `credit_limit`, `withdrawable`, and — on a **DERIVATIVE** account —
  `equity`, `excess_equity`, `initial_margin`, `maintenance_margin`. **This bullet previously read
  "❌ no cash / equity / margin split — `buying_power` only". That is no longer true.**
  🔑 **Absent means "this broker does not report it", NEVER zero.** Do not read a `None` as `0`.
* ✅ **liberator balance WORKS** — proven live against two funded accounts. Not exposed; not broken.
* ⛔ **liberator POSITIONS DO NOT WORK AT ALL, and a route would not help** — see the block below.
* ⚠️ **CORRECTED 2026-08-28 — this line conflated the CONTRACT with the VENUE.** It read: *"no cost
  basis, no market value, no unrealised P&L — `net_qty` only, and no marks anywhere in the
  contract"*. The **contract** part is true and unchanged: `adapters/base.Position` carries
  `account/market/symbol/net_qty/side` and no marks. But it was written as though it described what
  the **venue** has, and it reads that way — the first populated capture shows Liberator sends
  **`avg`** (cost basis), **`marketPrice`**, **`marketVal`**, **`unrealizedPL`** and
  **`unrealizedPLPercent`**. ⇒ the marks are **available and currently discarded**, which is a very
  different statement from "they do not exist", and it makes enriching `Position` a real option
  rather than a blocked one.
* ⚠️ **streaming_pro: balance covers SET + TFEX; POSITIONS are still SET-only**, per §5. Corrected
  2026-08-27 — this line previously read *"SET-only for BOTH"*, which the bridge's new `seosd` front
  superseded on the balance half the same day. Positions remain SET-only in the adapter.

> ⛔ **Why liberator positions are NOT a "just add the route" item — read this before planning around
> one.**
>
> `POST /va/portfolio` answers `result.{list, stock}`, and **neither array has ever been observed
> non-empty on this platform** — no Liberator account has ever held a position, so **the element
> schema (field names, types) has never been captured.** The previous implementation parsed a key the
> bridge does not emit and returned `[]` for every account **without raising**; replacing one invented
> parse with another would repeat that defect, so `get_positions` now **refuses loudly (501)**
> instead.
>
> ⇒ **The blocker is a missing capture, upstream of any route.** Shipping `GET /positions` today would
> return a 501 or an empty list, not positions. What would settle it: one funded account holding one
> position, captured once — [`docs/reference/liberator-account-reads.md`](../../docs/reference/liberator-account-reads.md) §7.

If your strategy needs P&L or marks, say so — those still require the contracts to grow, and that is a
bigger change than the two routes.

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

### 🔴 At `paper`, a GREEN test does not prove broker connectivity

**No order reaches any real broker at `paper`, regardless of which `broker` value you send.** Every
placement is intercepted to `SimAdapter` before the real adapter is consulted. So a passing
place/status/cancel run at `paper` proves **the contract and the durable-store path** — it proves
**nothing** about whether the broker session works.

> **The concrete illustration, from 2026-08-21 on this service.** A paper run produced three
> `liberator`-branded **FILLED** rows on a node that held **no Liberator credential at all**. The
> store records the **requested** broker, not the one contacted — so `broker: "liberator"` in a
> result row is *not* evidence that Liberator was reached. Nothing was wrong; that is the design. But
> a gate that reads those rows as broker proof is measuring the wrong thing.

⚠️ **Reads are the exception, and it cuts the other way:** at `paper`, `intent=READ` is **not**
intercepted — it goes to the **real** adapter and the real venue. So a balance read at `paper` touches
the live broker with real credentials. Treat it as a live call, not a rehearsal.

### 🔴 `micro_live` now REFUSES TO START without an EH6 declaration (new 2026-08-24, PR #38)

**`paper` is unaffected — you need nothing for it.** But a stage flip is no longer just a stage flip:

| | requirement |
|---|---|
| `sim`, `paper` | nothing new |
| `micro_live`, `live` | **`EXECUTION_ENGINE_REAL_ROUTING_ACCOUNTS`** must name the **8-digit trading accounts** this node is authoritative for — and a broker credential must back it |

**Absent ⇒ the app refuses to start.** A declaration that disagrees with the credentials the node
actually holds ⇒ **also refuses**, rather than picking one. An order for an undeclared account is
rejected at submit with a typed **`409 real_routing_not_authorized`** — deliberately distinct from
`stage_rejected`, so a log can tell *"the ladder said no"* from *"this node is not that account's
router"*.

This is **EH6**: exactly one authoritative real-router per broker account, because orders are not
idempotent across nodes and two routers on one account is double fills. ⚠️ Note the account **is the
8-digit trading account** (`<login><suffix>`), not the 7-digit login — they are one suffix apart.

---

## Related

[`api/orders-submit.md`](api/orders-submit.md) · [`api/orders-cancel.md`](api/orders-cancel.md) ·
[`api/orders-amend.md`](api/orders-amend.md) · [`api/orders-get.md`](api/orders-get.md) ·
[`api/orders-stream.md`](api/orders-stream.md) · [`api/capabilities.md`](api/capabilities.md) ·
[`architecture/adapters.md`](architecture/adapters.md) ·
[`../.claude/knowledge/capability-matrix.md`](../.claude/knowledge/capability-matrix.md)

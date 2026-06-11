# Broker research — Settrade Open API v2 (`settrade_v2` Python SDK)

> Source: the **official** `settrade_v2` Python SDK, v2.2.1 (PyPI wheel extracted + AST-parsed,
> 2026-06-09): modules `user.py`, `context.py`, `derivatives.py`, `realtime.py`, `errors.py`.
> This is the **Phase-4 `SettradeAdapter`** basis. Public-safe: no credentials reproduced.
>
> **Header note SUPERSEDED (2026-06-11):** the original note here said the hosted docs
> (`developer.settrade.com/.../sdkv2/python/investor-derivatives/…`) are a JS-rendered SPA that
> "could not be scraped." During the Phase 4 build we found the SPA serves its content as **raw
> markdown** from a `/template/open-api/...` backend (see "Venue docs scraping recipe" below) —
> so the venue docs ARE the authoritative reference for enum sets and wire shapes, and the SDK
> source corroborates them. Every former `(confirm P4)` cell is pinned from these two sources.

## Auth (OAuth-style app credentials)

- Entry point `Investor(app_id, app_secret, broker_id, app_code, …)` (`user.py`), then
  `.Derivatives(account_no)` → `InvestorDerivatives` (`derivatives.py`). (`MarketRep` variant
  also exists.)
- `Context` (`context.py`) logs in at
  `POST {base}/api/oam/v1/{broker_id}/broker-apps/{app_code}/login`, receiving
  `token_type` + `access_token` + `refresh_token`; **auto-refreshes** via
  `…/refresh-token` (`_should_refresh()` before each request). Requests carry
  `Authorization: {token_type} {token}`. There is a built-in **`RateLimit`** (per-endpoint
  blocks; `wait()` throttles). NTP time-sync (`sync_ntp_time_diff`) is handled by the SDK.
- Each order operation also takes a per-order **`pin`**.
- Implication: the OAuth session + rate-limit live **inside** `SettradeAdapter` (D10); the
  adapter must surface session-dead detection to the health/reconciliation path.

## `InvestorDerivatives` — order operations (verbatim signatures)

```python
place_order(pin, symbol, side, position, price, volume,
            price_type='Limit', iceberg_vol=None, validity_type='Day',
            validity_date_condition=None, stop_condition=None, stop_symbol=None,
            stop_price=None, trigger_session=None, bypass_warning=None)   # POST /{account_no}/orders
change_order(pin, order_no, new_price=None, new_volume=None, bypass_warning=None)  # NATIVE AMEND
cancel_order(order_no, pin)
cancel_orders(order_no_list, pin)
get_order(order_no)            # single
get_orders()                   # all
get_trades()                   # fills
get_portfolios()               # positions
get_account_info()             # account / buying power
```

- `place_order` POST body keys: `symbol`, `side`, `position` (Open/Close), `priceType`,
  `price`, `volume`, `icebergVol`, `validityType`, `validityDateCondition`, `stopCondition`,
  `stopSymbol`, `stopPrice`, `triggerSession`, `bypassWarning`, `pin` (None-valued keys are
  dropped before send).
- **Native amend** (`change_order`) is the key divergence from Liberator (which has none).
- **Stop** orders via `stop_condition`/`stop_symbol`/`stop_price`; **iceberg** via
  `iceberg_vol`.
- The exact **allowed enum values** for `price_type` / `validity_type` /
  `validity_date_condition` / `trigger_session` are **not constrained in the SDK** (passed as
  strings, validated server-side). They must be **pinned in Phase 4** against the live venue /
  current Settrade docs before the adapter declares them in the capability matrix — do not
  guess them in the contract. Defaults: `price_type='Limit'`, `validity_type='Day'`.

## Streaming — native order-update feed

- `RealtimeDataConnection` (`realtime.py`, MQTT-based) exposes
  **`subscribe_derivatives_order(...)`** (plus `subscribe_equity_order`,
  `subscribe_candlestick`, `subscribe_bid_offer`, `subscribe_price_info`,
  `subscribe_derivatives_exchange_info`, `subscribe_error`). The derivatives-order subscription
  is the **native push** that feeds the engine's normalized order-update stream in Phase 5 — no
  polling needed for Settrade (unlike Liberator).
- Protobuf message schemas live under `settrade_v2/pb/` (`orderdvv3_pb2`, `bidofferv3_pb2`, …).

## Idempotency / errors

- **No client idempotency key** — orders identified by broker `order_no` (int). Same as
  Liberator: the engine owns the `client_order_id ↔ order_no` mapping.
- Errors: base `SettradeError` (`errors.py`), plus HTTP error envelopes via `context.dispatch`.

## Adapter mapping summary (Phase 4)

| Normalized | Settrade derivatives |
|---|---|
| `broker=settrade`, `market=TFEX` | `InvestorDerivatives` (derivatives only in scope) |
| `side=BUY/SELL` | `side` Long/Short |
| `position_effect=OPEN/CLOSE` | `position` Open/Close |
| `order_type` | `price_type` (+ stop fields for STOP/STOP_LIMIT) — **pin enum P4** |
| `ICEBERG` (`display_qty`) | `iceberg_vol` |
| `tif` | `validity_type` (+ `validity_date_condition`) — **pin enum P4** |
| amend | **native** `change_order(new_price, new_volume)` |
| cancel | `cancel_order` / `cancel_orders` |
| reconcile | `get_order(s)` / `get_trades` / `get_portfolios` / `get_account_info` |
| order-update stream | `subscribe_derivatives_order` (native MQTT push) |

## Equity (`InvestorEquity`) surface — addendum (2026-06-11)

The Phase-3-era note above was **TFEX-only**; Phase 4 ships SET equity too (operator scope
amendment — see [decision-log E26](decision-log.md)). The equity book mirrors the derivatives
book with these venue-pinned differences (verbatim from the official docs + SDK; bodies drop
None-valued keys before send):

```text
# SET equity (/api/seos/v3/{broker_id}/accounts/{account_no}); trades on v4
POST   /orders          body: pin, side(Buy|Sell), symbol, trusteeIdType(Local|NVDR),
                              volume, qtyOpen, price, priceType, validityType,
                              clientType:"Individual", bypassWarning?, validTillDate?
PATCH  /orders/{order_no:str}/change   {pin, newTrusteeIdType?, newPrice?, newVolume?,
                                        newIcebergVolume?, bypassWarning?}  -> {} on success
PATCH  /orders/{order_no:str}/cancel   {pin}      ; bulk PATCH /cancel {pin, orders:[str]}
GET    /orders          order item: orderNo(str), accountNo, symbol, side, priceType, price,
                              vol, matched(cumulative), balance, cancelled, icebergVol,
                              status(e.g. 'CS'), showOrderStatus, rejectCode, rejectReason,
                              canCancel, canChangePriceVol, nvdrFlag, orderType, setOrderNo, ...
GET    /api/seos/v4/{broker_id}/accounts/{account_no}/trades   (equity trades live on v4)
```

- **`trusteeIdType`** = `Local | NVDR`; engine pins `'Local'` v1 (NVDR out of scope — no
  `NormalizedOrder` field).
- **`qtyOpen`** = iceberg display volume (0 = none) — the equity analogue of TFEX `icebergVol`.
- **`clientType`** = `"Individual"` (engine constant).
- **Native `change_order`** carries `new_trustee_id_type` / `new_iceberg_volume` on the wire in
  addition to `new_price` / `new_volume`; the engine deliberately sends only `newPrice`/`newVolume`
  in v1 (the trustee/iceberg amend fields are unexposed — see "Implemented vs researched").
- Trades item (v4): `orderNo`, `price`/`px`, `volume`/`qty`, `tradeId`, `side`, `tradeDate`,
  `tradeTime`, `status`, `rejectCode`, `rejectReason`. **Reserved for Phase 5** (the reconciler
  v1 uses cumulative-`matched` watermark deltas, not per-fill trades).

## Venue docs scraping recipe (supersedes the header's "SPA cannot be scraped" note)

The `developer.settrade.com` docs site is a JS SPA, but it renders its content from a **raw
markdown backend** under `/template/open-api/...` — directly fetchable, no browser needed:

```
menu / page index:  https://developer.settrade.com/template/open-api/sdkv2/python/investor-{derivatives,equity}/config.json
page bodies:        https://developer.settrade.com/template/open-api/sdkv2/python/investor-{derivatives,equity}/{n}_{name}.md
```

- `config.json` is the section menu (ordered page list); each entry maps to an `{n}_{name}.md`
  raw-markdown page (e.g. order placement, order types, validity, stop conditions).
- This is the **doc-pinning vehicle for future phases** — when a venue enum/wire detail must be
  confirmed (Phase 5 streaming protobuf, Phase 6 rate-limit headers, new order types), fetch the
  relevant `.md` rather than guessing or relying on the SDK passthrough strings. Pinned copies of
  the Phase-4 pages live alongside `/tmp/settrade_docs/PINNED.md`.

## Implemented vs researched (Phase 4)

What the live `adapters/settrade/` actually does, vs this research note:

- **Raw `httpx.AsyncClient`, not the SDK** (decision E21): the sync `settrade-v2` SDK is forbidden
  in `src/` (`requests`-based) and has import-time side effects (writes
  `~/settradesdkv2_config.txt`, NTP call, version-check HTTP). The wire is re-implemented; shapes
  pinned from SDK source + scraped venue docs. `cryptography>=42` added for ECDSA P-256 login.
- **Enum sets now pinned** (were SDK-passthrough strings — R4): SET `priceType`
  `Limit/MP-MKT/MP-MTL/ATO/ATC`, TFEX `Limit/MP-MKT/MP-MTL/ATO` + stop legs; both books
  `validityType` `Day/IOC/FOK/Cancel(GTC)` — `Date(GTD)` undeclared (no `Tif` member); TFEX
  `position` `Open/Close` (`Auto` undeclared); stop-condition derived from side
  (`BUY→LAST_PAID_OR_HIGHER`, `SELL→LAST_PAID_OR_LOWER`, `stopSymbol=symbol`). Full cells in
  [`capability-matrix.md`](capability-matrix.md).
- **Per-order account, not a constructor binding.** The SDK binds `account_no` at
  `.Derivatives(account_no)` construction; the engine takes the account from each
  `NormalizedOrder.account` and builds the path per call (`EXECUTION_ENGINE_SETTRADE_ACCOUNT_NO`
  is an integration-test convenience only).
- **Refresh-failure fallback improvement** (E21): the SDK silently ignores a failed refresh; our
  client falls back to a **fresh login** instead. Single-flight `ensure_token()` under one
  `asyncio.Lock`; one serial-guarded reactive-401 retry.
- **Rate limits observe-only** (E25): we parse `X-RateLimit-*` and budget-skip reconcile reads
  when the GET bucket is exhausted, but do NOT actively throttle the submit path (the SDK's
  `RateLimit.wait()` blocking-throttle is not copied). GET vs POST+PATCH are separate buckets.
- **`get_trades` reserved for Phase 5** — the reconciler v1 uses cumulative-`matched` watermark
  deltas (E18/E24), not per-fill trades; the native MQTT `subscribe_{derivatives,equity}_order`
  push feeds the normalized stream in Phase 5.
- **Out of scope (declared):** `MarketRep*` (broker-employee credential class), `MarketData` (the
  market-data plane — D1 split, stays in `quant-marketdata-engine`), `RealtimeDataConnection`/MQTT
  (Phase 5), `_place_orders` (private SDK batch method), NVDR, `Auto` position, `Date`(GTD) TIF,
  `SESSION`-trigger stops.

## InnovestX (broker 023) specifics (Phase 4.1)

The real broker **InnovestX (broker `023`)** splits its two markets across **two investor OAuth
apps** — `ALGO_EQ` for **SET equity**, `ALGO` for **TFEX derivatives** — each with its own
`app_id`/`app_secret`/`app_code`. One app cannot route both legs of a stock-vs-futures spread, so
Phase 4.1 made the engine support **per-market credentials** (one `SettradeClient` per market;
`EXECUTION_ENGINE_SETTRADE_{EQUITY,DERIVATIVES}_APP_*`). Validated **read-only against prod
2026-06-11** through the refactored adapter: both apps' tokens acquired; **equity accounts
`902001825` / `903001825`** read via the `ALGO_EQ` (SET) client, **TFEX `507619-0`** via the
`ALGO` (derivatives) client; no writes (the PIN is never serialized on reads). By contrast the
**UAT sandbox (broker `098`) is ONE app for both books** — the shared `settrade_app_*` trio resolves
to a single client keyed under both markets (one login, one session). The InnovestX trading PIN is
not yet in `.env` — the explicit prerequisite for flipping InnovestX to `micro_live`. See
[`capability-matrix.md`](capability-matrix.md) (per-market auth note) and
[`decision-log.md`](decision-log.md) E28.

# Broker research — Settrade Open API v2 (`settrade_v2` Python SDK)

> Source: the **official** `settrade_v2` Python SDK, v2.2.1 (PyPI wheel extracted + AST-parsed,
> 2026-06-09): modules `user.py`, `context.py`, `derivatives.py`, `realtime.py`, `errors.py`.
> The hosted docs (`developer.settrade.com/open-api/.../sdkv2/python/investor-derivatives/…`)
> are a JS-rendered SPA and could not be scraped; the **SDK source is the authoritative
> reference** and is quoted below. This is the **Phase-4 `SettradeAdapter`** basis. Public-safe:
> no credentials reproduced.

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

# Broker research — Liberator (`liberator-trading-api`)

> Source: the existing private service `github.com/lumduan/liberator-trading-api` (read from a
> scratch clone, 2026-06-09). This is the **Phase-3 `LiberatorAdapter` target** — the engine
> composes it over HTTP (Decision D9), it does not re-implement Liberator. Public-safe: no real
> account numbers / PINs / tokens reproduced here.

## Shape

A FastAPI service (`app/`) wrapping the Liberator web trading API. Router prefix `/api/v1`.
Order endpoints are split by market (SET equity, TFEX derivatives), each with place / cancel /
pre-place. Models are Pydantic with `errorCode`/`errMsg`/`result` envelopes.

## Auth / session

- **OTP / 2FA** login flow with an **SMS webhook** refresh; token + session persisted in
  **Redis** (`redis_auth_token_service`, `redis_otp_service`, `session_monitor_service`,
  `session_status_service`). Endpoints: `/auth` login, `/order` (per market), `/otp` SMS
  webhook, `/token`, `/session-status`, `/ws-ticket`.
- Each **order** call also requires a per-order **trading PIN** (6–10 digits).
- Implication for the adapter: the **session liveness** (OTP expiry + SMS-refresh) is
  irreducibly Liberator-specific (D10) and must be health-checked before routing.

## Order placement

- **SET:** `POST /api/v1/order/place/set` — `SETOrderRequest`:
  - `side: Buy | Sell`; `priceType: Limit | Market | MP | ATO | ATC`;
    `validityType: Day | GTC | IOC | FOK`; `icebergVol` (≥0); `volume` (1–100000);
    `price` (Decimal, 2 dp); `accountNo`; `pin`; `nvdr` flag. **No stop type on SET.**
- **TFEX:** `POST /api/v1/order/place/tfex` — `TFEXOrderRequest`:
  - `side: Long | Short`; `position: Open | Close | Auto` (**position effect**);
    `priceType: Limit | Market | Stop`; `validityType: Day | GTC | IOC | FOK`;
    `stopCondition` / `stopSymbol` / `stopPrice` (stop support); `icebergVol`;
    `volume` (1–10000); `price` (Decimal); `accountNo`; `pin`.
- **Pre-place** (cost calc): `POST /api/v1/order/pre-place/{set,tfex}` returns commission /
  regulatory-fee line items — useful for a pre-trade cost estimate.

## Cancel

- `POST /api/v1/order/cancelled/{set,tfex}` — `orderNo` is a **list** (numeric IDs or UUIDs),
  ≤ 50 at once, + `pin`. Cancellation is by venue order number.

## Amend

- **No amend / change endpoint exists in the service.** Order query (`OrderItem`) reports
  `canChangePriceVol` and `canCancel`, but there is **no route** to change price/volume.
  → `LiberatorAdapter.amend` must be **cancel-then-replace** (declared in its capability set as
  non-atomic at the venue).

## Query (reconciliation source)

- `GET /api/v1/orders/`, `GET /api/v1/orders/{account_no}`, `GET /api/v1/orders/summary`.
- `OrderItem` fields: `orderNo`, `status` + `statusShow`, `side` (`B`/`S`), `position`,
  `volume` / `matched` / `balance` / `cancelled`, `amount` / `fee` / `vat`, `rejectCode`,
  `priceType`, `validityType`, `stopPrice` / `stopSymbol`, `canCancel`, `canChangePriceVol`,
  UTC datetimes (`tradeDate`/`entryDate`/`tradeTime`/`entryTime`). This is the data the
  reconciliation loop maps onto the normalized status enum.

## Streaming

- `POST /api/v1/ws-ticket` issues a **WebSocket ticket** for the venue's streaming channel
  (market / order book). There is **no normalized order-update push** from this service — for
  Phase 5 the engine reconciles/poll-normalizes Liberator order state into the same
  order-update shape it emits for Settrade.

## Idempotency

- **None.** Orders are identified by the broker-assigned `orderNo` returned on ack; there is no
  client idempotency key. The **engine** owns the `client_order_id ↔ orderNo` mapping and
  dedupes before routing (D5).

## Status / error taxonomy

- Envelope: `errorCode` (0 = success) + `errMsg`; `is_success` when `errorCode == 0` and no
  `errMsg`. Per-order rejects carry a `rejectCode` on the `OrderItem`. The adapter maps these to
  the normalized `REJECTED` status + `reject_reason`.

## Adapter mapping summary (Phase 3)

| Normalized | Liberator (SET) | Liberator (TFEX) |
|---|---|---|
| `side=BUY/SELL` | `Buy/Sell` | `Long/Short` |
| `position_effect` | n/a (cash) | `Open/Close` (`Auto` available) |
| `order_type=MARKET/LIMIT` | `Market/Limit` | `Market/Limit` |
| `STOP/STOP_LIMIT` | ✗ | `priceType=Stop` + stop fields |
| `ICEBERG` (`display_qty`) | `icebergVol` | `icebergVol` |
| `ATO/ATC` | `ATO/ATC` | ✗ |
| `MTL` | `MP` | ✗ |
| `tif` | `Day/GTC/IOC/FOK` | `Day/GTC/IOC/FOK` |
| amend | cancel+replace | cancel+replace |
| cancel | `cancelled/set` (orderNo list) | `cancelled/tfex` (orderNo list) |

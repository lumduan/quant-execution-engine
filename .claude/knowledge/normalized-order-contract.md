# NormalizedOrder contract

> The single order language every strategy speaks. Pinned in Phase 0 (ADR) and realised as
> Pydantic models in Phase 2. `Decimal`-as-string on the wire; `int` quantities; UTC
> timestamps (display Asia/Bangkok). Canonical sketch:
> [`docs/plans/ROADMAP.md`](../../docs/plans/ROADMAP.md#normalizedorder--normalizedorderresult-contract-sketch--pinned-in-phase-02).

## Request — `NormalizedOrder`

| Field | Type | Notes |
|---|---|---|
| `client_order_id` | str | client-generated idempotency key (UUID/ULID); dedupe key |
| `broker` | `sim\|liberator\|settrade` | routing target |
| `account` | str | broker account ref; **never logged in full** |
| `market` | `SET\|TFEX` | |
| `symbol` | str | venue symbol, e.g. `PTT`, `S50H26` |
| `side` | `BUY\|SELL` | mapped per adapter (TFEX → Long/Short) |
| `order_type` | `MARKET\|LIMIT\|STOP\|STOP_LIMIT\|ICEBERG\|MTL\|ATO\|ATC` | validated vs capability matrix |
| `price` | Decimal? | required for LIMIT / STOP_LIMIT |
| `stop_price` | Decimal? | required for STOP / STOP_LIMIT |
| `quantity` | int | contracts (TFEX) or shares (SET) |
| `display_qty` | int? | iceberg display size |
| `tif` | `DAY\|IOC\|FOK\|GTC` | |
| `position_effect` | `OPEN\|CLOSE\|None` | TFEX only; None for SET cash |
| `metadata` | dict | opaque strategy tags; **never sent to a venue** |

## Result — `NormalizedOrderResult`

`client_order_id`, `broker_order_id?`, `broker`, `status` (enum), `filled_qty`,
`remaining_qty`, `avg_fill_price?` (Decimal), `reject_reason?` (mapped from broker
`reject_code`/`err_msg`), `created_at`/`updated_at` (UTC), `raw?` (private-only — never crosses
the public boundary).

## Status enum

`NEW | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | EXPIRED`

## Validation rules (router, before any venue I/O)

1. Dedupe on `client_order_id` — seen ⇒ return the prior `NormalizedOrderResult` (idempotent).
2. Capability check — reject unsupported `(broker, market, order_type, tif)` with a typed error.
3. Pre-trade risk gate — notional / qty / price-band / duplicate-burst caps + the kill-switch.
4. Stage gate — `EXECUTION_ENGINE_STAGE` must permit a real route (`live`); else `SimAdapter`.
5. Field requiredness — `price` for LIMIT/STOP_LIMIT, `stop_price` for STOP/STOP_LIMIT,
   `display_qty ≤ quantity` for ICEBERG, `position_effect` required for TFEX.

## Adapter side/position mapping

| Normalized | Liberator SET | Liberator TFEX | Settrade derivatives |
|---|---|---|---|
| `BUY` | `Buy` | `Long` | `Long` |
| `SELL` | `Sell` | `Short` | `Short` |
| `position_effect=OPEN/CLOSE` | n/a | `Open/Close` | `Open/Close` |

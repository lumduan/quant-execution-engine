# NormalizedOrder contract

> **FROZEN in Phase 0 (2026-06-10)** by the ADR
> ([`feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md),
> Pinned §C–§D — the source of truth); realised as Pydantic models in Phase 2. The single
> order language every strategy speaks. `Decimal`-as-string on the wire; `int` quantities;
> UTC timestamps (display Asia/Bangkok); no `float` at any money boundary. Canonical sketch:
> [`docs/plans/ROADMAP.md`](../../docs/plans/ROADMAP.md#normalizedorder--normalizedorderresult-contract-frozen-in-phase-0-realised-in-phase-2).

## Request — `NormalizedOrder`

| Field | Type | Notes |
|---|---|---|
| `client_order_id` | str | client-generated idempotency key — **UUIDv4 standard** (ADR §A; time-ordered drop-ins acceptable, never parsed for time); dedupe key |
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

## `BrokerAdapter` interface (frozen in Phase 0, ADR §D)

Every adapter (`SimAdapter`, `LiberatorAdapter`, `SettradeAdapter`) implements exactly:

```text
place(order: NormalizedOrder)                 -> NormalizedOrderResult
cancel(client_order_id: str)                  -> NormalizedOrderResult
amend(client_order_id: str,
      new_price?: Decimal, new_qty?: int)     -> NormalizedOrderResult
get_open_orders(account: str)                 -> list[NormalizedOrderResult]
get_positions(account: str)                   -> normalized positions
get_account(account: str)                     -> normalized account/buying-power
capabilities()                                -> per-(broker, market) capability sets
```

Amend semantics are **declared per adapter**, never assumed: Settrade amends natively
(`change_order`); `LiberatorAdapter.amend` is cancel-then-replace — two venue operations,
**non-atomic** (queue-priority loss + a brief no-resting-order window, declared in its
capability metadata). Callers query `capabilities()` to learn the semantics.

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

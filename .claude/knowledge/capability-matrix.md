# Capability matrix — Liberator vs Settrade vs Sim

> **Shape FROZEN in Phase 0 (2026-06-10)** by the ADR
> ([`feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md),
> Pinned §F — per-`(broker, market)` capability sets; this file stays the canonical cell-level
> matrix the ADR links). Reconciles the two broker research notes
> ([Liberator](broker-research-liberator.md), [Settrade](broker-research-settrade.md)) onto one
> `NormalizedOrder`. The router enforces these per-adapter capabilities **up front** (D7) —
> an unsupported `(broker, market, order_type, tif)` is rejected with a typed error before any
> venue I/O. **The former `(confirm P4)` Settrade cells were pinned in Phase 4 (2026-06-11)**
> from the official venue docs (R4 resolved) — no placeholder remains. Canonical copy lives in
> [`docs/plans/ROADMAP.md`](../../docs/plans/ROADMAP.md#broker-capability-matrix-liberator-vs-settrade-vs-sim).

| Capability | Liberator (SET / TFEX) | Settrade (SET + TFEX) | Sim |
|---|---|---|---|
| Auth | OTP/2FA + SMS refresh + Redis token; per-order PIN | OAuth app creds → token (ECDSA P-256 login sig, single-flight refresh, rate-limited); per-order PIN | none |
| Markets | SET + TFEX | SET (`/api/seos/v3`) + TFEX (`/api/seosd/v3`) | any |
| `side` | SET Buy/Sell; TFEX Long/Short | SET Buy/Sell; TFEX Long/Short | both |
| `position_effect` | TFEX Open/Close/Auto; SET n/a | TFEX Open/Close (`Auto` ✗); SET n/a | both |
| MARKET / LIMIT | ✅ | ✅ (`MP-MKT` / `Limit`) | ✅ |
| STOP / STOP_LIMIT | TFEX ✅; SET ✗ | TFEX ✅ (`MP-MKT`/`Limit` + stop trio); SET ✗ | ✅ |
| ICEBERG | ✅ icebergVol | ✅ SET `qtyOpen` / TFEX `icebergVol` | ✅ |
| ATO / ATC | SET ✅; TFEX ✗ | SET ✅ (`ATO`/`ATC`); TFEX `ATO` ✅, `ATC` ✗ | ✅ |
| MTL / MP | SET ✅ (MP); TFEX ✗ | SET + TFEX ✅ (`MP-MTL`) | ✅ |
| TIF | Day/GTC/IOC/FOK | Day/IOC/FOK/GTC(`Cancel`); `Date`(GTD) ✗ | all |
| **Amend** | ✗ no route → **cancel+replace** (non-atomic) | ✅ **native** `PATCH .../change` (`PENDING_REPLACE → NEW`) | ✅ |
| Cancel | orderNo list (≤50) + PIN | `PATCH .../cancel` + bulk `PATCH /cancel` + PIN | ✅ |
| Reconcile query | `GET /orders*` | `GET /orders` (cumulative matched, rejectCode/Reason, canCancel/canChange); `GET /trades` → Phase 5 | in-proc |
| Order-update stream | indirect (ws-ticket; engine normalizes by reconcile) | **native** `subscribe_{derivatives,equity}_order` (MQTT) — Phase 5 | synthetic |
| Client idempotency key | ✗ | ✗ | n/a |

## The two structural consequences

1. **Engine-owned idempotency.** Neither broker accepts a client key, so the engine persists
   `client_order_id ↔ broker_order_id` and dedupes before routing. Exactly-once-ish =
   dedupe + durable state + reconcile + safe re-submit (not true exactly-once).
2. **Asymmetric amend.** `BrokerAdapter.amend()` is uniform, but `LiberatorAdapter.amend`
   degrades to cancel-then-replace (declared non-atomic); `SettradeAdapter.amend` is native.
   Callers query `GET /capabilities` to learn the semantics, never assume them.

## Phase 3 validation status (2026-06-11)

The **Liberator column is now validated against the live adapter code** — every cell above is
realized 1:1 by `src/quant_execution_engine/adapters/liberator/mapping.py` (parametrized over
all valid `(market, order_type, side, position_effect, tif)` cells) and declared via
`CAPABILITY_MATRIX` with `adapter_installed: true` for both Liberator rows. Divergences and
wire details discovered during the build (research notes stay accurate; these are additions):

- **SET requires `price > 0` with ≤2 dp on EVERY order**, including the market family
  (`Market`/`MP`/`ATO`/`ATC`) — the upstream wire model mandates it. The adapter rejects
  pre-flight (no HTTP) when no indicative price can be coalesced or when a price exceeds
  2 dp (it never silently re-quantizes a limit). TFEX `price` accepts `0`.
- **TFEX `stopSymbol` is required on EVERY TFEX order** (defaulted to the order symbol);
  `stopCondition` ships `""` in v1 — the venue's condition vocabulary is undocumented upstream
  and gets pinned during operator-driven micro_live validation (a venue reject flows back
  typed, never silent).
- **Read-side `side` strings are `B`/`S`**, not the write-side `Buy`/`Sell`/`Long`/`Short` —
  two mapping directions exist and both are tested.
- **`position_effect=Auto` is not exposed** through `NormalizedOrder` (OPEN/CLOSE only, frozen
  §C); venue rows carrying `Auto` are unrepresentable in the read view and skipped.
- Amend stays **cancel+replace, non-atomic, with NO PTRM exemption** for the replacement
  (decision E17): queue-priority loss + a brief no-resting-order window + a possible
  duplicate-burst risk-reject inside the window are the declared consequences.

## Phase 4 validation status (2026-06-11)

The **Settrade column is now validated against the live adapter code + the official venue docs**
(`developer.settrade.com/.../investor-{derivatives,equity}/*.md`, the raw markdown backend,
cross-checked against the `settrade-v2` 2.2.1 SDK source). Both Settrade rows are declared via
`CAPABILITY_MATRIX` with `adapter_installed: true` and `amend: "native"`, and every cell is
realized 1:1 by `src/quant_execution_engine/adapters/settrade/mapping.py` (parametrized over all
valid SET + TFEX `(order_type, side, position_effect, tif)` cells). The former `(confirm P4)`
placeholders are gone.

**Pinned enum sets (the cells):**

- **SETTRADE × SET** (`/api/seos/v3`): order_types `LIMIT('Limit')`, `MARKET('MP-MKT')`,
  `MTL('MP-MTL')`, `ATO('ATO')`, `ATC('ATC')`, `ICEBERG('Limit' + qtyOpen=display_qty)`; tifs
  `DAY('Day')`, `IOC('IOC')`, `FOK('FOK')`, `GTC('Cancel')`; `position_effects=()`.
- **SETTRADE × TFEX** (`/api/seosd/v3`): order_types `LIMIT('Limit')`, `MARKET('MP-MKT')`,
  `MTL('MP-MTL')`, `ATO('ATO')`, `STOP('MP-MKT' + stop trio)`, `STOP_LIMIT('Limit' + price +
  stop trio)`, `ICEBERG('Limit' + icebergVol)`; same tifs; `position_effects=(OPEN('Open'),
  CLOSE('Close'))`.

**Explicitly UNSUPPORTED (rejected pre-flight, before any HTTP):** SET stops / SET STOP_LIMIT
(no equity stop API); TFEX `ATC` (not a derivatives priceType); `Date`(GTD) TIF (no `Tif`
member — no `validityDateCondition` path); `Auto` position effect (extra permission, undeclared);
NVDR (`trusteeIdType` pinned `'Local'` v1 — `NormalizedOrder` has no trustee field); `SESSION`-
trigger stops (no contract condition/`triggerSession` field).

**Wire-detail notes discovered/pinned during the build (additions; research stays accurate):**

- **`wire_price` float-exactness guard:** `Decimal → float` happens only when the float's `repr`
  round-trips back to the exact `Decimal`; otherwise a typed reject — the adapter **never
  re-quantizes** a limit price.
- **`price=0` wire rule:** `ATO`/`ATC`/`MP-MTL`/`MP-MKT` (and the `STOP` market leg) send
  `price: 0` on the wire.
- **Stop-condition derivation** (`NormalizedOrder` has `stop_price` but no condition field):
  `BUY → 'LAST_PAID_OR_HIGHER'`, `SELL → 'LAST_PAID_OR_LOWER'`, `stopSymbol = order.symbol`.
- **`trusteeIdType` pin:** every SET order ships `trusteeIdType='Local'` (NVDR is out of scope).
- **Read-side `side` strings** are the same `Buy/Sell` (SET) and `Long/Short` (TFEX) as the
  write side; fills are cumulative-watermark deltas off `matched`/`matchQty` (E18), keyed
  `broker_fill_id = f"{order_no}:{matched}"`.
- **Amend is `native`, atomic at the engine** (one `replace_order` over `PENDING_REPLACE → NEW`),
  kill-switch-gated up front, PTRM-rechecked with **no exemption**; a venue amend-reject is a
  NON-terminal restore + typed `AmendRejected` (409) — the order stays live (decisions E21–E27).

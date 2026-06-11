# Capability matrix — Liberator vs Settrade vs Sim

> **Shape FROZEN in Phase 0 (2026-06-10)** by the ADR
> ([`feature-execution-engine.md`](../../../.claude/knowledge/feature-execution-engine.md),
> Pinned §F — per-`(broker, market)` capability sets; this file stays the canonical cell-level
> matrix the ADR links). Reconciles the two broker research notes
> ([Liberator](broker-research-liberator.md), [Settrade](broker-research-settrade.md)) onto one
> `NormalizedOrder`. The router enforces these per-adapter capabilities **up front** (D7) —
> an unsupported `(broker, market, order_type, tif)` is rejected with a typed error before any
> venue I/O. **(confirm P4)** = a Settrade enum the SDK passes through as a string; pinned during
> the Phase-4 adapter build (deferred-by-design, R4), not guessed here. Canonical copy lives in
> [`docs/plans/ROADMAP.md`](../../docs/plans/ROADMAP.md#broker-capability-matrix-liberator-vs-settrade-vs-sim).

| Capability | Liberator (SET / TFEX) | Settrade (derivatives) | Sim |
|---|---|---|---|
| Auth | OTP/2FA + SMS refresh + Redis token; per-order PIN | OAuth app creds → token (auto-refresh, rate-limited); per-order PIN | none |
| Markets | SET + TFEX | TFEX | any |
| `side` | SET Buy/Sell; TFEX Long/Short | Long/Short | both |
| `position_effect` | TFEX Open/Close/Auto; SET n/a | Open/Close | both |
| MARKET / LIMIT | ✅ | ✅ (market variants confirm P4) | ✅ |
| STOP / STOP_LIMIT | TFEX ✅; SET ✗ | ✅ (stop_* fields) | ✅ |
| ICEBERG | ✅ icebergVol | ✅ iceberg_vol | ✅ |
| ATO / ATC | SET ✅; TFEX ✗ | confirm P4 | ✅ |
| MTL / MP | SET ✅ (MP); TFEX ✗ | confirm P4 | ✅ |
| TIF | Day/GTC/IOC/FOK | Day/IOC/FOK/Date… confirm P4 | all |
| **Amend** | ✗ no route → **cancel+replace** (non-atomic) | ✅ **native** `change_order` | ✅ |
| Cancel | orderNo list (≤50) + PIN | `cancel_order(s)` | ✅ |
| Reconcile query | `GET /orders*` | `get_order(s)/trades/portfolios/account_info` | in-proc |
| Order-update stream | indirect (ws-ticket; engine normalizes by reconcile) | **native** `subscribe_derivatives_order` (MQTT) | synthetic |
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

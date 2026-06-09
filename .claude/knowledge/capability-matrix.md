# Capability matrix — Liberator vs Settrade vs Sim

> Reconciles the two broker research notes
> ([Liberator](broker-research-liberator.md), [Settrade](broker-research-settrade.md)) onto one
> `NormalizedOrder`. The router enforces these per-adapter capabilities **up front** (D7) —
> an unsupported `(broker, market, order_type, tif)` is rejected with a typed error before any
> venue I/O. **(confirm P4)** = a Settrade enum the SDK passes through as a string; pinned during
> the Phase-4 adapter build, not guessed here. Canonical copy lives in
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

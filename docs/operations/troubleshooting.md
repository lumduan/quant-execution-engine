# Operations — Troubleshooting

Common failure modes, how to diagnose them, and how to resolve them. Start every diagnosis with the
health probe:

```bash
curl http://localhost:8400/health        # stage, public_mode, brokers, order_book
```

## Broker circuit breaker tripped — `broker_circuit_open` (503)

**Symptom:** placements to a broker reject with `broker_circuit_open`; `/health` shows
`brokers.<b>.breaker_state: "open"`. **Cause:** consecutive heartbeat failures tripped the breaker,
which also mass-cancelled that broker's open orders. **Resolve:** investigate the broker session
(Liberator OTP session expiry / upstream down; Settrade OAuth token / app health), confirm
credentials, and let the heartbeat recover the breaker (or restart the adapter runtime). For the
Settrade per-market split, `brokers.settrade.sessions` shows which app (`SET`/`TFEX`) is dead.

## `PENDING_NEW` stuck / ack lost (§B)

**Symptom:** an order sits in `engine_state: "PENDING_NEW"` after submit. **Cause:** the broker ack
was lost before the `broker_order_id` was recorded. **Resolve:** the reconciliation loop fuzzy-matches
the venue's open orders on `(account, symbol, side, quantity)` within **±5 s** of the submit time and
acks the order to `NEW`; if no unique match is found it resolves to `REJECTED` after a bounded
`ack_lost_unmatched` window (~60 s). No operator action — the loop never blindly re-sends. Check the
per-order audit:

```bash
curl http://localhost:8400/admin/orders/<cid>/audit -H "X-API-Key: <your-api-key>"
```

## `PENDING_REPLACE` stranded (Settrade amend)

**Symptom:** a native amend left the order in `engine_state: "PENDING_REPLACE"`. **Cause:** the amend
request was sent but the ack was lost. **Resolve:** the Settrade reconciler's `replace_resolve` action
restores the order to `NEW` with the venue's current resting values. Confirm via the per-order audit
(a `replace_request` then a `replace`/`ack` row).

## `duplicate_burst_detected` (409)

**Symptom:** a submit is rejected with `duplicate_burst_detected`. **Cause:** a second order carrying
the same economic fingerprint `account|symbol|side|quantity|order_type|price` under a **different**
`client_order_id` arrived within `EXECUTION_ENGINE_DUPLICATE_BURST_WINDOW_SECONDS` (default 5). This is
the guard working. **Resolve:** if intentional, wait for the window to expire or vary the order; if it
is a buggy retry, reuse the **same** `client_order_id` (idempotency dedupe returns the prior ack — no
409). The guard is default-ON; disable only deliberately via
`EXECUTION_ENGINE_DUPLICATE_BURST_GUARD_ENABLED=false`.

## `price_band_exceeded` (422)

**Symptom:** a LIMIT order is rejected with `price_band_exceeded`. **Cause:** `PRICE_BAND_ENABLED=true`
and the limit price is more than `PRICE_BAND_MAX_PCT`% off the symbol's last close. **Resolve:** if a
false alarm, check `EXECUTION_ENGINE_MARKET_DATA_BASE_URL` reachability (a fetch *failure* is advisory
WARN+pass — a *reject* means the price really is out of band). MARKET orders bypass the check.

## DB down (`quant-postgres`)

**Symptom:** write paths (submit/cancel/amend) fail; the order store is unreachable. **Cause:**
`quant-infra-db` Postgres is down or the DSN is wrong. **Resolve:** the durable store is the source of
truth, so the engine cannot route without it — bring `quant-infra-db` up first, then restart the
engine. Read endpoints may still serve recently cached state.

## Redis down (`quant-execution-redis`)

**Symptom:** dedupe / single-flight lock / rate-limit paths fail. **Cause:** the engine's own Redis
sidecar is down. **Resolve:** restart the `quant-execution-redis` sidecar. The single-flight lock
guards against concurrent identical `client_order_id` double-submit — without it, a tight concurrent
retry could race; bring Redis back before resuming load.

## Gateway `502` / `503` / `504` on `/api/v2/engines/execution/*`

**Diagnose in order:** (1) the engine directly — `curl http://localhost:8400/health`; (2) the gateway
logs; (3) the network — both services must be on `quant-network`. `504` = upstream timeout, `503` =
engine unreachable, `502` = bad upstream response. The gateway holds no credential, so a gateway error
is never a credential problem — it is reachability.

## Settrade order book — `DISPATCH-UM-04 "User is inactive"`

**Symptom:** enabling the order book with the Settrade provider yields `DISPATCH-UM-04 "User is
inactive"`. **Cause:** realtime market data is **not enabled at the InnovestX portal** for the account
— an operator prerequisite, not a code issue. **Resolve:** enable realtime at the portal, or set
`EXECUTION_ENGINE_ORDER_BOOK_PRIMARY_PROVIDER=liberator` (the verified-live default). The order book is
default-off regardless (`ORDER_BOOK_ENABLED=false`).

## Liberator session dead / `session.relogin_otp_timeout` alert

**Symptom:** Liberator routing has halted (`brokers.liberator.breaker_state: "open"`), and the bundled
liberator container logged a structured **ERROR** `session.relogin_otp_timeout`
(`trigger=immediate_login|reconnection wait_seconds=N`). **Cause:** the auto-relogin monitor detected a
dead broker session (the JWT expires ~24 h with no refresh token), fired a re-login + OTP SMS, but the
OTP was **not confirmed** within `RELOGIN_OTP_WAIT_SECONDS` — almost always because the operator's
**iPhone OTP-forward automation** isn't running, so the SMS reached the phone but wasn't forwarded to
`POST /api/v1/otp/sms`. The session **stays dead by design** (never a false "alive"). **Resolve:** check
the iPhone automation is online; once healthy, the next monitor poll (or a manual `POST /api/v1/login/`
on the internal upstream) re-triggers — confirm with `GET /api/v1/session/status` = alive. Full
behaviour + config: [`liberator-session-self-heal.md`](liberator-session-self-heal.md). **Note:** a
session that dies over a weekend / holiday stays dead until market open (the §C trading-hours gate
pauses the monitor), then auto-heals.

> **Deploy gotcha — rebuild after a pin bump.** The liberator container is built from the submodule
> source; advancing the `third_party/liberator-trading-api` pin (or editing
> `docker/liberator/session_status.yaml`) does **not** redeploy the running image. Rebuild:
> `docker compose -f docker-compose.yml -f docker-compose.private.yml -f docker-compose.liberator.yml build liberator-trading-api && … up -d`.

## `live` stage rejected — `stage_rejected` (403)

**Symptom:** a submit with `EXECUTION_ENGINE_STAGE=live` is rejected. **Cause:** `live` is **gated** —
there is no real-money default. **Resolve:** this is intended. Real routing is `micro_live` only,
operator-driven; see [`kill-switch.md`](kill-switch.md) (the stage-flip rule) and the umbrella runbook.

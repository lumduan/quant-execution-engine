# Operations — Bring-up

The engine joins the external `quant-network` and writes the `execution.*` store in `quant-infra-db`.
**Bring up `quant-infra-db` first** — it creates the network and applies the execution schema.

## Prerequisite: `quant-infra-db`

```bash
cd ../quant-infra-db && docker compose up -d   # creates quant-network + db_execution
```

The `execution.*` schema is **owned by `quant-infra-db`** and applied from its init-scripts when the
database first initializes: `12_schema_execution.sql` (the order store + triggers) and
`13_execution_strategy_id.sql` (the Phase-5 `strategy_id` column + index). The engine never owns or
migrates this schema; it only reads/writes it. See [`../data/execution-schema.md`](../data/execution-schema.md).

## The three compose configurations

### a. Public / sim default (broker-free)

```bash
docker compose up -d            # host :8400, SimAdapter only, PUBLIC_MODE=true, STAGE=sim
```

No broker credentials, no real venue, order-submission endpoints disabled. The engine + its own Redis
sidecar (`quant-execution-redis`) come up. This is the safe default.

### b. Owner mode + Settrade (cloud API)

```bash
docker compose -f docker-compose.yml -f docker-compose.private.yml up -d
```

The private overlay flips `PUBLIC_MODE=false` (order-submission enabled) and loads broker credentials
from the gitignored `.env` (`env_file: .env`). Settrade is a **cloud API — no overlay service**; its
OAuth creds ride this overlay's `.env`. Real routing still requires `EXECUTION_ENGINE_STAGE=micro_live`
(or higher, gated) **and** the kill-switch disengaged.

### c. Owner mode + bundled Liberator upstream

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.private.yml \
               -f docker-compose.liberator.yml up -d
```

The Liberator overlay bundles `liberator-trading-api` as an **internal-only** upstream (no host port,
not a peer service on `quant-network`) plus its own `liberator-redis` sidecar. The engine reaches it at
`http://liberator-trading-api:8200/api/v1`. The nested submodule lives at
`third_party/liberator-trading-api` — a fresh checkout needs `git submodule update --init`.

## Bring-up order (relative to infra-db & gateway)

```
quant-infra-db          # creates quant-network + Postgres (db_execution) — must be first
quant-execution-engine  # this service + its own Redis sidecar (host :8400)
quant-api-gateway       # proxies /api/v2/engines/execution/* → :8400
strategies (csm-set, tfex-s50-multi-tf-swing)   # submit normalized orders behind a flag
```

## Health check

```bash
curl http://localhost:8400/health
```

```json
{ "status": "ok", "service": "quant-execution-engine", "version": "0.6.0",
  "stage": "sim", "public_mode": true, "brokers": null, "order_book": null }
```

In owner mode with brokers configured, `brokers` carries each broker's breaker + session state; see
[`../api/health.md`](../api/health.md).

## Tear-down

Tear down in reverse; **only** `quant-infra-db` down removes the `quant-network`:

```bash
docker compose down                                 # (or with the same -f overlays you brought it up)
cd ../quant-infra-db && docker compose down         # removes quant-network
```

## Fresh-clone gotcha

The nested `third_party/liberator-trading-api` is a private git submodule. After cloning the umbrella,
run `git submodule update --init --recursive` (or `--init` inside this repo) before the Liberator
overlay will build — a `-` prefix in `git submodule status` means it failed to populate (usually
missing GitHub https credentials). The public/sim default (config **a**) does not need it.

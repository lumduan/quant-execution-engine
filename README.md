# quant-execution-engine

> Execution engine — canonical order router + **sole owner of broker order-routing
> credentials**; gateway-proxied (host `:8400`).

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

`quant-execution-engine` is the [quant-trading-system](https://github.com/lumduan/quant-trading-system)
platform's **Execution engine**: a standalone `EXTERNAL` FastAPI service that is the **only**
thing that sends orders to brokers. Every strategy submits one canonical `NormalizedOrder`; the
engine routes it to a broker adapter (Liberator, Streaming Pro) or the `SimAdapter`. Strategies
**never** speak a broker's native order API and **never** hold a broker credential.

> **Status: scaffolded — all phases Proposed (2026-06-09).** This repo is a FastAPI skeleton
> exposing only `GET /health`. The order-routing surface, adapters, durable state machine, and
> reconciliation are **not implemented yet**. The build sequence is
> [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md).

## Role in the engine-based architecture

```
strategies ──NormalizedOrder──► quant-api-gateway (proxy, no credential)
                                      │  /api/v2/engines/execution/*
                                      ▼
                            quant-execution-engine  (host :8400, container :8000)
                               BrokerAdapter ┌─ SimAdapter
                               + state machine ├─ LiberatorAdapter ──HTTP─► liberator-trading-api
                               + idempotency   └─ StreamingProAdapter ─HTTP─► settrade-streaming-api bridge
                                      │ durable order store
                                      ▼
                            quant-infra-db (execution.orders / fills / order_events)
```

It follows the same standalone, gateway-proxied, **sole-credential-owner** pattern as
`quant-marketdata-engine` — but for the **execution plane** (orders), never the streaming plane.

## Network & ports

| Item | Value |
|---|---|
| Container port | `:8000` |
| Host port | `:8400` |
| Docker network | external **`quant-network`** (created by `quant-infra-db`) |
| Own Redis sidecar | `quant-execution-redis` (dedupe / single-flight submit lock / rate-limit) |
| Durable order store | `quant-postgres` → `db_execution` (`execution.*`) |
| Gateway proxy surface | `/api/v2/engines/execution/*` (orders, status, capabilities, order-update WS) |
| Health check | `curl http://localhost:8400/health` |

## Safety first

Order routing is irreversible. The `EXECUTION_ENGINE_STAGE` ladder
(`sim` → `paper` → `micro_live` → `live`, **default `sim`**) gates real-money routing; the
global kill-switch (`EXECUTION_ENGINE_KILL_SWITCH_ENGAGED`) overrides every stage; public mode
(Docker default) disables all order-submission endpoints. Broker secrets live **only** in a
gitignored `.env` — never committed, never logged. See the ROADMAP's "Safety ladder" section
and [`.claude/playbooks/order-routing-safety.md`](.claude/playbooks/order-routing-safety.md).

## Bring-up order

```bash
# from the umbrella repo
cd quant-infra-db        && docker compose up -d   # creates quant-network + Postgres
cd ../quant-execution-engine && docker compose up -d   # this service + own Redis (host :8400)
cd ../quant-api-gateway  && docker compose up -d   # proxies /api/v2/engines/execution/*

# owner mode (broker creds via gitignored .env; live routing still stage-gated):
docker compose -f docker-compose.yml -f docker-compose.private.yml up -d

curl http://localhost:8400/health
```

## Development

```bash
uv sync --all-groups
uv run uvicorn src.quant_execution_engine.api.main:app --port 8000
```

Quality gate (matches CI — must pass before every push):

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
```

`ruff` (E, F, I, UP, B, SIM) · `mypy --strict` · `pytest` with **≥90% coverage** on core
modules (`adapters/` + the order state machine as they land).

## Documentation

- **Roadmap (8 phases, 0–7):** [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md)
- **Agent guide:** [`CLAUDE.md`](CLAUDE.md)
- **Architecture ADR (Phase-0 gate, D1–D13):** umbrella
  `.claude/knowledge/feature-execution-engine.md`
- **Broker research + capability matrix + contract + state machine:** [`.claude/knowledge/`](.claude/knowledge/)

## Security

Broker credentials are never committed or logged. Report vulnerabilities privately to
**bad.sonsuk@gmail.com** rather than opening a public issue. See [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).

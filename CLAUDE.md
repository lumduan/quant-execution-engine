# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this
repository.

## Project

`quant-execution-engine` is the platform's **Execution engine** — a standalone `EXTERNAL`
engine that is the **single canonical order router** and the **sole owner of broker
order-routing credentials**. It is a FastAPI service on container port `:8000` (host port
`:8400`) that joins the external **`quant-network`** and is **proxied by `quant-api-gateway`**
under `/api/v2/engines/execution/*`. It writes a durable order store (`execution.*` in
`quant-infra-db`/TimescaleDB) and ships its **own Redis sidecar** (dedupe / single-flight
submit lock / rate-limit).

> **Current state: Phase 0 complete — ADR ACCEPTED (2026-06-10); Phases 1–7 Proposed.** The
> repo is a FastAPI skeleton from `lumduan/python-template` exposing only `GET /health`; the
> order-routing surface, adapters, and state machine are **not implemented yet**. The
> contracts (D1–D13, `NormalizedOrder`, `BrokerAdapter`, state machine, capability-matrix
> shape) are **frozen** in the umbrella ADR
> [`.claude/knowledge/feature-execution-engine.md`](../.claude/knowledge/feature-execution-engine.md);
> the build sequence is [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md) (8 phases, 0–7).
> Next: **Phase 1** — the `execution` schema, a `quant-infra-db` PR.

### Ownership boundaries (the whole point of this service)

1. **Sole broker-credential owner.** Only this service holds broker order-routing sessions
   (Liberator OTP/PIN, Settrade OAuth `app_id`/`app_secret`/`app_code`). No strategy, no
   gateway, and no host holds them. Secrets live **only** in this service's gitignored `.env`
   — never committed, never logged.
2. **Canonical order router.** Strategies submit one `NormalizedOrder`; the engine routes it
   to a `BrokerAdapter` (Liberator, Settrade) or `SimAdapter`. Add a broker = write one
   adapter, not touch every strategy.
3. **Gateway-proxied.** Consumers (strategies, OpenBB) call the gateway's
   `/api/v2/engines/execution/*`; the gateway proxies to `:8400` and holds **no** credential.
4. **Strategies never speak a broker API.** They POST normalized orders behind a flag and
   react to the normalized order-update stream; they never hold a credential.

## The two planes (do not merge them)

This is the **execution / order-command plane only** (low-volume, real-money, idempotent,
durable). The **market-data / streaming plane** (ticks, order book, OBI replay) is a separate
concern that stays in `order-book-infrastructure` + `quant-marketdata-engine`. We may *read*
those feeds for price-band pre-trade checks; we never own them. Rationale: a dropped tick is a
resubscribe, a duplicated order is a real loss.

## `NormalizedOrder` contract + status enum (frozen in Phase 0)

`NormalizedOrder(client_order_id, broker, account, market=SET|TFEX, symbol, side=BUY|SELL,
order_type=MARKET|LIMIT|STOP|STOP_LIMIT|ICEBERG|MTL|ATO|ATC, price?, stop_price?, quantity,
display_qty?, tif=DAY|IOC|FOK|GTC, position_effect?=OPEN|CLOSE)`. Status enum:
`NEW | PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED | EXPIRED`. `Decimal`-as-string on the
wire; UTC timestamps. Full sketch + state machine: [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md)
and [`.claude/knowledge/normalized-order-contract.md`](.claude/knowledge/normalized-order-contract.md).

## Safety ladder (`EXECUTION_ENGINE_STAGE`) — the most important rule

`sim` (default) → `paper` → `micro_live` → `live`. **No order reaches a real broker until the
stage is explicitly `live` AND the global kill-switch is disengaged AND owner mode is on.** The
kill-switch (`EXECUTION_ENGINE_KILL_SWITCH_ENGAGED`) overrides every stage and is checked first
in the submit path. Public mode (`EXECUTION_ENGINE_PUBLIC_MODE=true`, Docker default) disables
all order-submission endpoints. See the ROADMAP's "Safety ladder" section.

## Network & ports (`quant-network`)

| Item | Value |
|---|---|
| Service hostname (in-container) | `quant-execution-engine` |
| Container port | `:8000` (always — like every other service) |
| Host port | `:8400` |
| Health check | `curl http://localhost:8400/health` |
| Durable order store | `quant-postgres:5432` (`execution.*`, `db_execution`) |
| Own Redis sidecar | in this repo's compose (`quant-execution-redis`, distinct from the gateway's Redis) |
| Gateway proxy surface | `POST\|GET\|DELETE /api/v2/engines/execution/*` (orders, status, capabilities, order-update WS) |

Use **service hostnames inside containers**, not `localhost`. Host ports exist only for
developer access.

## Commands

Everything runs through `uv`. Never call `python` / `pip` / `poetry` / `conda` directly.

```bash
uv sync --all-groups                                  # install deps (incl. dev)
uv run pytest                                         # full test suite + coverage gate
uv run ruff check .                                   # lint
uv run ruff format --check .                          # format check (passive)
uv run mypy src tests                                 # strict type check
uv run uvicorn src.quant_execution_engine.api.main:app --port 8000   # run the API
```

Combined quality gate (must pass before every push, matching CI):

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
```

Docker:

```bash
docker compose up                                                     # public mode, host :8400
docker compose -f docker-compose.yml -f docker-compose.private.yml up # owner mode (broker creds via .env)
```

## Quality gates

`ruff` (E, F, I, UP, B, SIM) · `mypy --strict` · `pytest` with **≥90% coverage on core
modules** (`--cov-fail-under=90`), enforced in CI and `pyproject.toml`. As the order path
lands, ≥90% applies specifically to `adapters/` + the order state machine.

## Bring-up order (relative to infra-db & gateway)

```
quant-infra-db          # creates quant-network + Postgres/TimescaleDB (must be first)
quant-execution-engine  # this service + its own Redis sidecar (host :8400)
quant-api-gateway       # proxies /api/v2/engines/execution/* → :8400
strategies (csm-set, tfex-s50-multi-tf-swing)   # submit normalized orders behind a flag
```

Tear down in reverse; only `quant-infra-db` down removes `quant-network`.

## Hard rules — service-specific

1. **Broker credentials live only here**, in a gitignored `.env`. **Never commit; never log.**
   The gateway and strategies hold none.
2. **Idempotency is mandatory.** Every order carries a client-generated `client_order_id`; the
   engine dedupes **before** routing. Re-submitting the same id returns the prior ack.
3. **Kill-switch overrides everything** and is checked first in the submit path. Sim is the
   default stage; `live` is gated and off by default.
4. **Adapters declare capabilities; the router enforces them.** Reject unsupported
   `(broker, market, order_type, tif)` up front with a typed error — never fail silently at a
   venue. Liberator has **no amend route** → `LiberatorAdapter.amend` is cancel-then-replace
   (declared, non-atomic); Settrade amends natively.
5. **Durable state + reconciliation.** Persist the lifecycle to `execution.orders` /
   `.fills` / append-only `.order_events` before anything reaches a venue; a reconciliation
   loop repairs submit/ack drift against broker truth.
6. **Order-submission endpoints are private/owner-mode** — public mode answers only health,
   capabilities, and reads. Raw broker payloads never cross the public boundary.

## Hard rules — inherited from the umbrella

1. **Always `uv run`** — never bare `python` / `pip` / `poetry` / `conda`.
2. **Async-first I/O** — all HTTP via `httpx.AsyncClient`. `requests` is forbidden in `src/`.
3. **Pydantic at boundaries** — module/external I/O goes through Pydantic models, never raw
   dicts.
4. **Monetary values are `Decimal`, never `float`,** at boundaries; serialise as strings on
   the wire. Prices are `numeric(18,6)` in the DB.
5. **Timezone:** store UTC, display `Asia/Bangkok`.
6. **No secrets in repo.** All config via env + `pydantic-settings`, prefix `EXECUTION_ENGINE_*`.
7. **Ingestion/submission is idempotent** (see service rule 2).
8. **`docs/plans/` is git-tracked.** The roadmap is part of the product — never gitignore it.

## Coding conventions worth knowing up front

- `from __future__ import annotations` at the top of every `src/` module.
- Module-local exceptions in each subpackage's `errors.py`, inheriting a shared base. Never
  `raise Exception(...)` or `except Exception: pass`.
- `logger = logging.getLogger(__name__)` — never `print` in `src/`; `%`-formatting in logs.
  **Never log a PIN, token, account number, or order payload secret.**
- File-size target ≤ 400 lines; functions ≤ ~50 lines.
- Tests mirror the source layout under `tests/`; `asyncio_mode = "auto"`. Internal imports use
  the `src.quant_execution_engine.…` prefix (matches `pythonpath = ["."]`).

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`,
`test:`, `chore:`, `refactor:`. Keep scope tight (`feat(adapters): Liberator place_order map`).

## Where to look next

- **Roadmap (source of truth for what to build next):** [`docs/plans/ROADMAP.md`](docs/plans/ROADMAP.md)
- **Architecture ADR (Phase-0 gate, D1–D13):** [`../.claude/knowledge/feature-execution-engine.md`](../.claude/knowledge/feature-execution-engine.md)
- **Broker research (cited):** [`.claude/knowledge/broker-research-liberator.md`](.claude/knowledge/broker-research-liberator.md),
  [`.claude/knowledge/broker-research-settrade.md`](.claude/knowledge/broker-research-settrade.md)
- **Capability matrix / contract / state machine:** [`.claude/knowledge/capability-matrix.md`](.claude/knowledge/capability-matrix.md),
  [`.claude/knowledge/normalized-order-contract.md`](.claude/knowledge/normalized-order-contract.md),
  [`.claude/knowledge/order-state-machine.md`](.claude/knowledge/order-state-machine.md)
- **Decision log:** [`.claude/knowledge/decision-log.md`](.claude/knowledge/decision-log.md)
- **Order-routing safety playbook:** [`.claude/playbooks/order-routing-safety.md`](.claude/playbooks/order-routing-safety.md)
- **Pattern precedent (standalone credential-owner engine):** `../quant-marketdata-engine/CLAUDE.md`
- **Umbrella system map:** [`../CLAUDE.md`](../CLAUDE.md)

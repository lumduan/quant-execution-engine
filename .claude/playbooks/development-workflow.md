# Playbook — development workflow

Day-to-day development for `quant-execution-engine`. For the irreversible-action checklist (anything
that could route a real order) see [`order-routing-safety.md`](order-routing-safety.md).

## Quality gate (must pass before every push, matches CI)

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest
```

- Everything runs through `uv` — never bare `python` / `pip` / `poetry` / `conda`.
- `mypy --strict`; `ruff` (E, F, I, UP, B, SIM); `pytest` with **≥90% coverage on core modules**
  (`adapters/` + the order state machine), `--cov-fail-under=90`.
- **Re-run `ruff format --check` after any post-format edit** (a stray `sed`/Edit invalidates a prior
  format pass). Do not push red.

## Branch naming

| Kind | Pattern | Example |
|------|---------|---------|
| Feature / phase | `feature/phase<N>-<slug>` | `feature/phase6-safety-ops-reconciliation-hardening` |
| Docs / phase | `docs/phase<N>-<slug>` | `docs/phase7-documentation` |
| Fix | `fix/<slug>` | `fix/phase3-liberator-overlay-config` |

Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`); keep scope tight
(`feat(adapters): Settrade native amend`).

## Development bring-up

`quant-infra-db` first (it creates `quant-network` + `db_execution`), then this service:

```bash
cd ../quant-infra-db && docker compose up -d        # network + Postgres + the execution schema
cd ../quant-execution-engine && docker compose up -d # public/sim default, host :8400
```

Run the API locally (outside compose) against an already-up Postgres/Redis:

```bash
uv run uvicorn src.quant_execution_engine.api.main:app --port 8000
```

See [`../knowledge/deployment.md`](../knowledge/deployment.md) for the owner/Liberator overlays.

## Running tests without live broker credentials

**No live credentials are ever needed (or allowed) in CI.** All broker HTTP is **`respx`-mocked**;
the `SimAdapter` is in-process; Settrade/Liberator transports are stubbed. The default
`uv run pytest` touches no real venue and no real Postgres unless a test is explicitly marked.

- Broker integration skeletons are `@pytest.mark.integration` and excluded from the default run / CI.
- A caplog test asserts "PIN never logged" — keep it green on any transport change.
- Never put a real `.env` / credential in a test; `respx` routes cover the wire.

## Python 3.11 CI gotcha (verify before push)

**CI runs a Python 3.11 + 3.12 matrix, but a local venv is often 3.13.** Async / timing / stress
tests (e.g. the EventHub slow-subscriber stress, rate-limit token-bucket timing) can pass on 3.13 /
3.12 yet fail on **3.11** due to event-loop scheduling differences. Before pushing anything that
touches async timing:

```bash
uv run --python 3.11 pytest tests/<the_async_or_timing_test>.py
```

Treat a green local 3.13 run as **necessary but not sufficient** for timing-sensitive tests — confirm
3.11 explicitly.

## Submodule discipline (the bundled Liberator upstream)

`third_party/liberator-trading-api` is a nested private submodule. Edits to it require a
**submodule-first** commit + push, **then** a pin bump + commit + push in this repo — never commit
this repo against an unpushed submodule SHA. A fresh checkout needs
`git submodule update --init --recursive`.
